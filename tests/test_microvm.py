"""microVM sandbox: the host side against a fake firecracker, the guest init's job handling,
and (with AIOS_TEST_MICROVM=1) the real thing: Firecracker on KVM. See docs/microvm-sandbox.md."""
import asyncio
import json
import os
import platform
import re
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_microvm_init as guest  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
POSIX = os.name == "posix"

# Stands in for firecracker: checks the config it is given, then plays the guest by running the
# real init's job code (without the privileged parts) and printing to "serial" with CRLF.
FAKE_FC = textwrap.dedent("""\
    #!{python}
    import json, os, sys, tempfile, time
    sys.path.insert(0, {repo!r})
    import aios_microvm_init as g
    args = sys.argv[1:]
    cfg = json.load(open(args[args.index("--config-file") + 1]))
    with open(os.environ["FAKE_FC_SEEN"], "w") as f:
        root = [d for d in cfg["drives"] if d["drive_id"] == "rootfs"][0]["path_on_host"]
        json.dump({{"args": args, "config": cfg, "cwd": os.getcwd(), "rootfs_ino": os.stat(root).st_ino}}, f)
    mode = os.environ.get("FAKE_FC_MODE", "")
    print("[    0.000000] Linux version fake", flush=True)
    if mode == "hang":
        time.sleep(600)
    if mode == "crash":
        print("Error: KVM_CREATE_VM failed", flush=True); sys.exit(1)
    job = [d for d in cfg["drives"] if d["drive_id"] == "job"][0]["path_on_host"]
    nonce, timeout, max_out, code = g.parse_job(open(job, "rb").read())
    if mode == "forged":
        print("AIOS-BEGIN-" + "0" * 32 + "\\nZm9yZ2Vk\\nAIOS-END-" + "0" * 32 + " status=ok rc=0 truncated=0")
        sys.exit(0)
    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as run:
        status, rc, out, trunc = g.run_code(code, timeout, max_out, work, run, unprivileged=False)
    sys.stdout.write(g.result_lines(nonce, status, rc, out, trunc).replace("\\n", "\\r\\n"))
    """)


# Stands in for firecracker with its API (snapshots): with --config-file it "boots" and waits
# like the real init in snapshot mode; without, it waits for /snapshot/load, then for the line on
# stdin, then runs the job drive named in the snapshot. Every step is appended to FAKE_FC_SEEN.log.
FAKE_FC_API = textwrap.dedent("""\
    #!{python}
    import json, os, socket, sys, tempfile
    sys.path.insert(0, {repo!r})
    import aios_microvm_init as g
    args = sys.argv[1:]
    mode = os.environ.get("FAKE_FC_MODE", "")
    def note(**kw):
        with open(os.environ["FAKE_FC_SEEN"] + ".log", "a") as f:
            f.write(json.dumps({{"cwd": os.getcwd(), **kw}}) + "\\n")
    if "--api-sock" not in args:
        print("no api", flush=True); sys.exit(2)
    cfg = json.load(open(args[args.index("--config-file") + 1])) if "--config-file" in args else None
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(args[args.index("--api-sock") + 1])
    srv.listen(4)
    def requests():
        while True:
            c, _ = srv.accept()
            f = c.makefile("rb")
            line = f.readline().decode().split()
            n = 0
            while (h := f.readline()) not in (b"\\r\\n", b""):
                if h.lower().startswith(b"content-length:"):
                    n = int(h.split(b":")[1])
            yield c, line[0], line[1], json.loads(f.read(n) or b"{{}}")
    def answer(c, code=204, body=b""):
        c.sendall(b"HTTP/1.1 %d X\\r\\nContent-Length: %d\\r\\n\\r\\n%s" % (code, len(body), body))
        c.close()
    if cfg is not None:  # boot for a snapshot
        note(step="boot", config=cfg)
        if mode == "noready":  # an init older than snapshots: reads the empty job and fails
            print("AIOS-INIT-ERROR JobError: bad job header", flush=True); sys.exit(0)
        print("[    0.000000] Linux version fake\\r\\n\\r\\nAIOS-READY\\r", flush=True)
        for c, method, path, body in requests():
            if (method, path) == ("PUT", "/snapshot/create"):
                json.dump({{"drives": cfg["drives"]}}, open(body["snapshot_path"], "w"))
                open(body["mem_file_path"], "wb").write(b"m" * 4096)
                note(step="create", body=body)
            answer(c)
    for c, method, path, body in requests():
        if (method, path) != ("PUT", "/snapshot/load"):
            answer(c, 400, b'{{"fault_message":"unexpected"}}'); continue
        if mode == "badload":
            answer(c, 400, b'{{"fault_message":"bad snapshot"}}'); sys.exit(1)
        state = json.load(open(body["snapshot_path"]))
        assert open(body["mem_backend"]["backend_path"], "rb").read() == b"m" * 4096
        answer(c)
        break
    go = sys.stdin.readline()
    job = [d for d in state["drives"] if d["drive_id"] == "job"][0]["path_on_host"]
    data = open(job, "rb").read()
    nonce, timeout, max_out, code = g.parse_job(data)
    note(step="load", body=body, go=go, job_size=len(data), seed=g.parse_extras(data)["seed"].hex(),
         links=sorted(n for n in os.listdir(".") if os.path.islink(n)))
    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as run:
        status, rc, out, trunc = g.run_code(code, timeout, max_out, work, run, unprivileged=False)
    sys.stdout.write(g.result_lines(nonce, status, rc, out, trunc).replace("\\n", "\\r\\n"))
    """)


# Stands in for the jailer (which needs root): checks its arguments, then does what it does
# minus chroot, uid switch and cgroups: run the exec file inside <base>/firecracker/<id>/root.
FAKE_JAILER = textwrap.dedent("""\
    #!{python}
    import json, os, sys
    args = sys.argv[1:]
    mine, rest = args[:args.index("--")], args[args.index("--") + 1:]
    opt = {{mine[i]: mine[i + 1] for i in range(0, len(mine) - 1, 2)}}
    with open(os.environ["FAKE_FC_SEEN"] + ".jailer", "w") as f:
        json.dump(mine, f)
    root = os.path.join(opt["--chroot-base-dir"], "firecracker", opt["--id"], "root")
    os.chdir(root)
    os.execv(opt["--exec-file"], [opt["--exec-file"], "--id", opt["--id"], *rest])
    """)


def vmm_processes():
    """Processes whose command line names a per-call work dir (that is, a running VMM)."""
    found = []
    for p in Path("/proc").glob("[0-9]*"):
        try:
            if b"aios-mvm-" in (p / "cmdline").read_bytes():
                found.append(p.name)
        except OSError:
            pass
    return found


def make_home(tmp: Path, fc=True, kernel=True, rootfs=True, jailer=False, api=False) -> Path:
    if fc:
        p = tmp / "firecracker"
        p.write_text((FAKE_FC_API if api else FAKE_FC).format(python=sys.executable, repo=str(REPO)))
        p.chmod(0o755)
    if kernel:
        (tmp / "vmlinux").write_bytes(b"kernel")
    if rootfs:
        (tmp / "rootfs.ext4").write_bytes(b"rootfs")
    if jailer:
        p = tmp / "jailer"
        p.write_text(FAKE_JAILER.format(python=sys.executable))
        p.chmod(0o755)
    return tmp


class GuestJobTests(unittest.TestCase):
    def test_round_trip(self):
        box = aios.MicroVMSandbox(timeout=7, max_out=99)
        img = box.job_image("ab" * 16, "print('hé')")
        self.assertEqual(len(img) % 512, 0)
        self.assertEqual(guest.parse_job(img), ("ab" * 16, 7, 99, "print('hé')".encode()))

    def test_rejects_malformed_jobs(self):
        good = b"AIOS1 " + b"a" * 32 + b" 5 100 3\nabc"
        self.assertEqual(guest.parse_job(good)[3], b"abc")
        for bad in (b"", b"no newline", b"AIOS2 " + b"a" * 32 + b" 5 100 3\nabc",
                    b"AIOS1 short 5 100 3\nabc", b"AIOS1 " + b"g" * 32 + b" 5 100 3\nabc",
                    b"AIOS1 " + b"a" * 32 + b" 0 100 3\nabc",
                    b"AIOS1 " + b"a" * 32 + b" 5 100 9\nabc",
                    b"AIOS1 " + b"a" * 32 + b" 5 x 3\nabc"):
            with self.subTest(bad=bad), self.assertRaises(guest.JobError):
                guest.parse_job(bad)

    def test_code_size_is_bounded(self):
        with self.assertRaises(ValueError):
            aios.MicroVMSandbox(max_code=10).job_image("a" * 32, "x" * 11)

    def test_each_job_carries_a_fresh_seed_and_the_time(self):
        box = aios.MicroVMSandbox()
        a, b = (box.job_image("ab" * 16, "print(1)") for _ in range(2))
        ea, eb = guest.parse_extras(a), guest.parse_extras(b)
        self.assertEqual(len(ea["seed"]), 32)
        self.assertNotEqual(ea["seed"], eb["seed"])
        self.assertLess(abs(ea["time"] - time.time()), 60)
        self.assertEqual(guest.parse_job(a)[3], b"print(1)")  # an older init still reads it
        self.assertEqual(guest.parse_extras(b"AIOS1 " + b"a" * 32 + b" 5 100 3\nabc"), {})

    def test_a_snapshot_job_has_the_fixed_size(self):
        box = aios.MicroVMSandbox()
        img = box.job_image("ab" * 16, "x" * (1 << 20), box.SNAPSHOT_JOB_SIZE)
        self.assertEqual(len(img), box.SNAPSHOT_JOB_SIZE)
        self.assertEqual(len(guest.parse_job(img)[3]), 1 << 20)
        self.assertIn("seed", guest.parse_extras(img))
        with self.assertRaises(ValueError):
            box.job_image("ab" * 16, "x" * 10, 100)


class MicroVMSelectionTests(unittest.TestCase):
    def test_never_chosen_automatically_and_explicit_choice_never_degrades(self):
        self.assertIn("microvm", aios.SANDBOXES)
        with mock.patch.object(aios.MicroVMSandbox, "unavailable", return_value=None):
            self.assertNotEqual(aios.make_sandbox("auto") and aios.make_sandbox("auto").name,
                                "microvm")
        with mock.patch.object(aios.MicroVMSandbox, "unavailable", return_value="no KVM"):
            with self.assertRaisesRegex(RuntimeError, "--sandbox microvm: no KVM"):
                aios.make_sandbox("microvm")
        with mock.patch.object(aios.MicroVMSandbox, "unavailable", return_value=None):
            self.assertEqual(aios.make_sandbox("microvm").name, "microvm")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_unavailable_explains_what_is_missing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"AIOS_MICROVM_JAILER": ""}), \
                mock.patch.object(aios.os.path, "exists", return_value=True), \
                mock.patch.object(aios.os, "access", side_effect=lambda p, m: p == "/dev/kvm" or Path(p).exists()):
            home = Path(d)
            with mock.patch.object(aios.shutil, "which", return_value=None):
                self.assertIn("firecracker not found", aios.MicroVMSandbox.unavailable(home))
            make_home(home, kernel=False, rootfs=False)
            self.assertIn("kernel image missing", aios.MicroVMSandbox.unavailable(home))
            make_home(home, fc=False, rootfs=False)
            self.assertIn("rootfs image missing", aios.MicroVMSandbox.unavailable(home))
            make_home(home, fc=False, kernel=False)
            self.assertIsNone(aios.MicroVMSandbox.unavailable(home))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_the_jailer_needs_root_and_its_binary(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(aios.os.path, "exists", return_value=True), \
                mock.patch.object(aios.os, "access", side_effect=lambda p, m: p == "/dev/kvm" or Path(p).exists()), \
                mock.patch.object(aios.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {"AIOS_MICROVM_JAILER": "1"}):
            home = make_home(Path(d))
            with mock.patch.object(aios.os, "geteuid", create=True, return_value=1000):
                self.assertIn("needs AIOS to run as root", aios.MicroVMSandbox.unavailable(home))
            with mock.patch.object(aios.os, "geteuid", create=True, return_value=0):
                self.assertIn("jailer not found", aios.MicroVMSandbox.unavailable(home))
                make_home(home, jailer=True)
                self.assertIsNone(aios.MicroVMSandbox.unavailable(home))
            self.assertTrue(aios.MicroVMSandbox(home).jailed)
        with mock.patch.dict(os.environ, {"AIOS_MICROVM_JAILER": ""}):
            self.assertFalse(aios.MicroVMSandbox(home).jailed)  # opt-in only

    def test_unavailable_without_kvm(self):
        with mock.patch.object(aios.sys, "platform", "linux"), \
                mock.patch.object(aios.os.path, "exists", return_value=False):
            self.assertIn("/dev/kvm is missing", aios.MicroVMSandbox.unavailable())
        with mock.patch.object(aios.sys, "platform", "win32"):
            self.assertIn("Linux with KVM", aios.MicroVMSandbox.unavailable())


@unittest.skipUnless(POSIX, "the fake firecracker is a POSIX script")
class MicroVMHostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        (self.tmp / "home").mkdir()
        self.home = make_home(self.tmp / "home")
        self.seen = self.tmp / "seen.json"
        env = mock.patch.dict(os.environ, {"FAKE_FC_SEEN": str(self.seen), "FAKE_FC_MODE": ""})
        env.start()
        self.addCleanup(env.stop)

    def box(self, **kw):
        return aios.MicroVMSandbox(home=self.home, **{"jailed": False, "snapshot": False, **kw})

    def workdirs(self):
        return sorted(Path(tempfile.gettempdir()).glob("aios-mvm-*"))

    async def test_runs_code_and_cleans_up(self):
        before = self.workdirs()
        out = await self.box().run_python("import sys; print(6*7); sys.exit(3)")
        self.assertEqual(out, "exit=3\n42\n")
        self.assertEqual(self.workdirs(), before)  # job image and config removed

    async def test_a_jailed_vmm_runs_in_its_own_chroot_that_is_removed_after(self):
        make_home(self.home, jailer=True)
        box = self.box(jailed=True, mem_mb=128)
        out = await box.run_python("print(6*7)")
        self.assertEqual(out, "exit=0\n42\n")
        jailer = json.loads(Path(str(self.seen) + ".jailer").read_text())
        opt = dict(zip(jailer[::2], jailer[1::2]))
        self.assertEqual((opt["--uid"], opt["--gid"]), ("65534", "65534"))
        self.assertEqual(opt["--chroot-base-dir"], str(self.home / "jails"))
        self.assertRegex(opt["--id"], r"^aios-mvm-[0-9a-f]{16}$")
        if Path("/sys/fs/cgroup/cgroup.controllers").exists():
            self.assertEqual(opt["--cgroup"], f"memory.max={(128 + box.JAIL_OVERHEAD_MB) << 20}")
        seen = json.loads(self.seen.read_text())
        self.assertEqual(Path(seen["cwd"]).name, "root")
        self.assertEqual(seen["config"]["boot-source"]["kernel_image_path"], "vmlinux")  # inside the chroot
        self.assertEqual([d["path_on_host"] for d in seen["config"]["drives"]], ["rootfs.ext4", "job.img"])
        self.assertEqual(seen["rootfs_ino"], (self.home / "rootfs.ext4").stat().st_ino)  # linked, not copied
        self.assertEqual(list((self.home / "jails" / "firecracker").iterdir()), [])  # the jail is gone

    async def test_config_has_no_network_and_read_only_drives(self):
        await self.box(mem_mb=128, vcpus=1).run_python("print(1)")
        seen = json.loads(self.seen.read_text())
        cfg = seen["config"]
        self.assertEqual(set(cfg), {"boot-source", "drives", "machine-config"})
        self.assertTrue(all(d["is_read_only"] for d in cfg["drives"]))
        self.assertEqual([d["drive_id"] for d in cfg["drives"]], ["rootfs", "job"])
        self.assertEqual(cfg["machine-config"]["mem_size_mib"], 128)
        self.assertIn("init=/aios-init", cfg["boot-source"]["boot_args"])
        self.assertIn("--no-api", seen["args"])
        self.assertTrue(Path(seen["cwd"]).name.startswith("aios-mvm-"))

    async def test_output_is_bounded(self):
        out = await self.box(max_out=100).run_python("print('x' * 100000)")
        self.assertEqual(out, "exit=0\n" + "x" * 100 + "\n...(truncated)")

    async def test_guest_timeout_is_reported_as_killed(self):
        out = await self.box(timeout=1).run_python("import time\nprint('started', flush=True)\n"
                                                   "time.sleep(30)")
        self.assertTrue(out.startswith("killed: timeout after 1s"), out)
        self.assertIn("started", out)

    async def test_host_deadline_kills_a_hung_vmm(self):
        os.environ["FAKE_FC_MODE"] = "hang"
        before = self.workdirs()
        t = time.monotonic()
        out = await self.box(timeout=1, boot_timeout=1).run_python("print(1)")
        self.assertLess(time.monotonic() - t, 10)
        self.assertTrue(out.startswith("killed: timeout after 1s"), out)
        self.assertEqual(self.workdirs(), before)

    async def test_vmm_failure_is_an_error_not_a_result(self):
        os.environ["FAKE_FC_MODE"] = "crash"
        with self.assertRaisesRegex(RuntimeError, "(?s)no result.*exit=1.*KVM_CREATE_VM"):
            await self.box().run_python("print(1)")

    async def test_a_block_without_this_calls_nonce_is_ignored(self):
        os.environ["FAKE_FC_MODE"] = "forged"
        with self.assertRaisesRegex(RuntimeError, "no result"):
            await self.box().run_python("print(1)")

    async def test_cancellation_kills_the_vmm(self):
        os.environ["FAKE_FC_MODE"] = "hang"
        before = self.workdirs()
        task = asyncio.create_task(self.box(timeout=30).run_python("print(1)"))
        for _ in range(100):
            if self.seen.exists():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.workdirs(), before)
        self.assertEqual(vmm_processes(), [])


@unittest.skipUnless(sys.platform.startswith("linux"), "snapshots use /proc/self/fd and Unix sockets")
class MicroVMSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        (self.tmp / "home").mkdir()
        self.home = make_home(self.tmp / "home", api=True)
        self.seen = self.tmp / "seen.json"
        env = mock.patch.dict(os.environ, {"FAKE_FC_SEEN": str(self.seen), "FAKE_FC_MODE": ""})
        env.start()
        self.addCleanup(env.stop)
        snaps = mock.patch.object(aios.MicroVMSandbox, "_snapshots", {})
        snaps.start()
        self.addCleanup(snaps.stop)

    def box(self, **kw):
        return aios.MicroVMSandbox(home=self.home, **{"jailed": False, "snapshot": True, **kw})

    def steps(self):
        log = Path(str(self.seen) + ".log")
        return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []

    async def test_one_boot_then_every_call_restores(self):
        box = self.box()
        self.assertEqual(await box.run_python("print(6*7)"), "exit=0\n42\n")
        self.assertEqual(await box.run_python("import sys; sys.exit(3)"), "exit=3\n")
        steps = self.steps()
        self.assertEqual([s["step"] for s in steps], ["boot", "create", "load", "load", "load"])  # probe + 2
        boot = steps[0]["config"]
        self.assertIn("aios_wait=1", boot["boot-source"]["boot_args"])
        self.assertEqual([d["path_on_host"] for d in boot["drives"]], ["rootfs.ext4", "job.img"])  # relative
        self.assertEqual(set(boot), {"boot-source", "drives", "machine-config"})
        snap = self.home / "snapshots" / box.snapshot_key()
        for name in ("vm.snap", "vm.mem", "job.img"):
            self.assertEqual((snap / name).stat().st_mode & 0o777, 0o444)
        self.assertEqual(sorted(os.listdir(snap)), ["job.img", "vm.mem", "vm.snap"])
        loads = steps[2:]
        self.assertTrue(all(s["go"] == "go\n" for s in loads))
        self.assertTrue(all(s["job_size"] == box.SNAPSHOT_JOB_SIZE for s in loads))
        self.assertEqual(len({s["seed"] for s in loads}), 3)  # every restored VM gets its own
        self.assertEqual(loads[0]["links"], ["rootfs.ext4", "vm.mem", "vm.snap"])
        self.assertTrue(all(Path(s["cwd"]).name.startswith("aios-mvm-") for s in loads))
        self.assertTrue(all(not Path(s["cwd"]).exists() for s in loads))  # per-call dirs removed

    async def test_a_new_image_makes_a_new_snapshot_and_drops_the_old(self):
        box = self.box()
        await box.run_python("print(1)")
        old = box.snapshot_key()
        (self.home / "rootfs.ext4").write_bytes(b"rootfs v2")
        self.assertNotEqual(box.snapshot_key(), old)
        self.assertEqual(await box.run_python("print(2)"), "exit=0\n2\n")
        self.assertEqual(os.listdir(self.home / "snapshots"), [box.snapshot_key()])
        # a snapshot already on disk is reused by a new process
        aios.MicroVMSandbox._snapshots.clear()
        self.assertEqual(await self.box().run_python("print(3)"), "exit=0\n3\n")
        self.assertEqual([s["step"] for s in self.steps()].count("boot"), 2)

    async def test_an_init_without_snapshot_support_means_booting_each_call(self):
        os.environ["FAKE_FC_MODE"] = "noready"
        make_home(self.home, api=False)  # the cold boot path, with the classic fake
        home_fc = self.home / "firecracker"
        api_fc = self.tmp / "fc-api"
        api_fc.write_text(FAKE_FC_API.format(python=sys.executable, repo=str(REPO)))
        api_fc.chmod(0o755)
        # snapshot creation runs the API fake; per-call boots run the classic fake
        real = aios.asyncio.create_subprocess_exec
        def pick(*argv, **kw):
            if "--api-sock" in argv and "--config-file" in argv:
                argv = (str(api_fc), *argv[1:])
            return real(*argv, **kw)
        with mock.patch.object(aios.asyncio, "create_subprocess_exec", pick), \
                mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            box = self.box()
            self.assertEqual(await box.run_python("print(1)"), "exit=0\n1\n")
            self.assertEqual(await box.run_python("print(2)"), "exit=0\n2\n")
        self.assertEqual(err.getvalue().count("snapshots off"), 1)
        self.assertIn("stopped before it was ready", err.getvalue())
        self.assertEqual([s["step"] for s in self.steps()], ["boot"])  # tried once, not per call
        self.assertFalse((self.home / "snapshots").exists() and os.listdir(self.home / "snapshots"))
        self.assertTrue(home_fc.exists())

    async def test_a_restore_that_fails_falls_back_before_the_job_is_sent(self):
        box = self.box()
        self.assertEqual(await box.run_python("print(1)"), "exit=0\n1\n")
        os.environ["FAKE_FC_MODE"] = "badload"
        with mock.patch.object(box, "_call", wraps=box._call) as call, \
                mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            with self.assertRaisesRegex(RuntimeError, "no result"):  # the API fake cannot boot
                await box.run_python("print(2)")
        self.assertIn("bad snapshot", err.getvalue())
        self.assertEqual([c.args[1] for c in call.call_args_list][-1], None)  # booted instead
        self.assertIsInstance(aios.MicroVMSandbox._snapshots[box.snapshot_key()], str)

    async def test_snapshots_can_be_turned_off(self):
        with mock.patch.dict(os.environ, {"AIOS_MICROVM_SNAPSHOT": "0"}):
            self.assertFalse(aios.MicroVMSandbox(home=self.home).snapshot)
        with mock.patch.dict(os.environ, {"AIOS_MICROVM_SNAPSHOT": ""}):
            self.assertTrue(aios.MicroVMSandbox(home=self.home).snapshot)

    async def test_a_jailed_vmm_restores_from_links_in_its_chroot(self):
        make_home(self.home, fc=False, kernel=False, rootfs=False, jailer=True)
        box = self.box(jailed=True)
        self.assertEqual(await box.run_python("print(5)"), "exit=0\n5\n")
        load = self.steps()[-1]
        self.assertEqual(Path(load["cwd"]).name, "root")
        self.assertEqual(load["links"], [])  # hard links, not symlinks out of the jail
        self.assertEqual(list((self.home / "jails" / "firecracker").iterdir()), [])

    async def test_cancelling_a_restored_call_kills_the_vmm(self):
        box = self.box(timeout=30)
        await box.run_python("print(1)")
        task = asyncio.create_task(box.run_python("import time; time.sleep(60)"))
        for _ in range(200):
            if [s for s in self.steps() if s["step"] == "load"][1:]:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(vmm_processes(), [])


@unittest.skipUnless(os.environ.get("AIOS_TEST_MICROVM") == "1",
                     "set AIOS_TEST_MICROVM=1 with KVM, firecracker, vmlinux and rootfs.ext4 "
                     "in $AIOS_MICROVM_DIR to run against real microVMs")
class RealMicroVMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        why = aios.MicroVMSandbox.unavailable()
        if why:
            self.fail(why)
        self.box = aios.MicroVMSandbox(timeout=20)

    async def run_json(self, code, box=None):
        out = await (box or self.box).run_python(code)
        self.assertTrue(out.startswith("exit=0\n"), out)
        return json.loads(out.split("\n", 1)[1])

    async def test_separate_kernel_and_unprivileged_user(self):
        got = await self.run_json(
            "import os, json\n"
            "status = dict(l.split(':', 1) for l in open('/proc/self/status') if ':' in l)\n"
            "print(json.dumps({'kernel': os.uname().release, 'ids': [os.getuid(), os.getgid()],\n"
            " 'groups': os.getgroups(), 'nnp': int(status['NoNewPrivs']),\n"
            " 'caps': [int(status[k], 16) for k in ('CapEff', 'CapPrm')]}))\n")
        self.assertNotEqual(got["kernel"], platform.release())  # its own kernel, not the host's
        self.assertEqual(got["ids"], [65534, 65534])
        self.assertEqual(got["groups"], [])
        self.assertEqual(got["nnp"], 1)
        self.assertEqual(got["caps"], [0, 0])

    async def test_no_network_device(self):
        got = await self.run_json(
            "import os, json, socket\n"
            "try:\n socket.create_connection(('1.1.1.1', 53), timeout=3); net = True\n"
            "except OSError: net = False\n"
            "print(json.dumps({'ifaces': sorted(os.listdir('/sys/class/net')), 'net': net}))\n")
        self.assertEqual(got, {"ifaces": ["lo"], "net": False})

    async def test_read_only_root_and_no_host_files(self):
        got = await self.run_json(
            "import os, json\n"
            "mounts = {p[1]: p[3].split(',') for p in map(str.split, open('/proc/mounts'))}\n"
            "def can_read(p):\n"
            "    try: open(p, 'rb').read(1); return True\n"
            "    except OSError: return False\n"
            "print(json.dumps({'root_ro': 'ro' in mounts['/'], 'tmp': os.listdir('/tmp'),\n"
            " 'job_readable': can_read('/dev/vdb'), 'run_readable': os.access('/run', os.R_OK),\n"
            " 'disks': sorted(d for d in os.listdir('/sys/block') if d.startswith('vd')),\n"
            " 'tmp_bytes': os.statvfs('/tmp').f_blocks * os.statvfs('/tmp').f_frsize,\n"
            " 'init_visible': os.path.exists('/proc/1/cmdline')}))\n")
        self.assertTrue(got["root_ro"])
        self.assertEqual(got["tmp"], [])
        self.assertFalse(got["job_readable"])  # the nonce and job stay with the init
        self.assertFalse(got["run_readable"])
        self.assertEqual(got["disks"], ["vda", "vdb"])  # root image and job drive only
        self.assertLessEqual(got["tmp_bytes"], 16 * 1024 * 1024)
        self.assertFalse(got["init_visible"])  # hidepid: other users' processes are hidden

    async def test_memory_is_capped_by_the_vm(self):
        got = await self.run_json(
            "import json\n"
            "total = int([l for l in open('/proc/meminfo') if l.startswith('MemTotal')][0].split()[1])\n"
            "print(json.dumps({'total_kib': total}))\n")
        self.assertLess(got["total_kib"], 256 * 1024)
        out = await self.box.run_python("x = bytearray(512 * 1024 * 1024)\nprint('BIG')")
        self.assertNotIn("BIG", out)  # MemoryError or the guest OOM killer; the host is untouched

    async def test_timeout_and_cleanup(self):
        box = aios.MicroVMSandbox(timeout=3)
        tmp = Path(tempfile.gettempdir())
        before = sorted(tmp.glob("aios-mvm-*"))
        t = time.monotonic()
        out = await box.run_python("while True: pass")
        self.assertTrue(out.startswith("killed: timeout after 3s"), out)
        self.assertLess(time.monotonic() - t, 3 + box.boot_timeout + box.cleanup_timeout)
        self.assertEqual(sorted(tmp.glob("aios-mvm-*")), before)
        self.assertEqual(vmm_processes(), [])

    async def test_restored_calls_are_fresh_and_faster_than_booting(self):
        if not self.box.snapshot:
            self.skipTest("AIOS_MICROVM_SNAPSHOT=0: every call boots")
        code = ("import os, json, time\nleft = os.listdir('/tmp')\nopen('/tmp/mine', 'w').write('x')\n"
                "print(json.dumps({'left': left, 'rand': os.urandom(16).hex(), 'time': time.time()}))\n")
        await self.run_json(code)  # makes the snapshot if this process has none yet
        self.assertIsInstance(aios.MicroVMSandbox._snapshots.get(self.box.snapshot_key()), Path,
                              aios.MicroVMSandbox._snapshots)
        runs, took = [], []
        for _ in range(3):
            t = time.monotonic()
            runs.append(await self.run_json(code))
            took.append(time.monotonic() - t)
        t = time.monotonic()
        await self.run_json(code, aios.MicroVMSandbox(timeout=20, snapshot=False))
        cold = time.monotonic() - t
        print(f"\n[evidence] restored calls {', '.join(f'{x:.3f}s' for x in took)}; booted call {cold:.3f}s",
              file=sys.stderr)
        self.assertEqual([r["left"] for r in runs], [[]] * 3)  # nothing from an earlier call
        self.assertEqual(len({r["rand"] for r in runs}), 3)  # the clones do not share random state
        for r in runs:
            self.assertLess(abs(r["time"] - time.time()), 30)  # the clock was set, not the snapshot's
        self.assertLess(max(took), cold)

    async def test_fork_bomb_and_output_flood_are_contained(self):
        out = await aios.MicroVMSandbox(timeout=5, max_out=1000).run_python(
            "import os\nprint('y' * 50000, flush=True)\n"
            "for _ in range(200):\n"
            "    try: os.fork()\n"
            "    except OSError: break\n")
        self.assertTrue(out.startswith(("exit=", "killed: timeout")), out)
        self.assertIn("...(truncated)", out)



@unittest.skipUnless(os.environ.get("AIOS_TEST_MICROVM") == "1" and os.environ.get("AIOS_MICROVM_JAILER") == "1",
                     "set AIOS_TEST_MICROVM=1 and AIOS_MICROVM_JAILER=1, as root, to run jailed microVMs")
class RealJailedMicroVMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        why = aios.MicroVMSandbox.unavailable()
        if why:
            self.fail(why)
        self.box = aios.MicroVMSandbox(timeout=20)
        self.assertTrue(self.box.jailed)

    def vmm(self):
        for pid in vmm_processes():
            try:
                if os.readlink(f"/proc/{pid}/exe").endswith("firecracker"):
                    return pid
            except OSError:
                pass
        return None

    async def test_the_vmm_is_chrooted_unprivileged_and_capped(self):
        run = asyncio.ensure_future(self.box.run_python("import time; time.sleep(4); print('done')"))
        for _ in range(200):
            pid = self.vmm()
            if pid:
                break
            await asyncio.sleep(0.05)
        self.assertIsNotNone(pid, "no running VMM found")
        status = dict(l.split(":", 1) for l in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in l)
        # The jailer pivot_roots in a new mount namespace, so from here /proc/PID/root reads "/":
        # look through it instead, and find the jail id from the VMM's cgroup.
        own_mounts = os.stat(f"/proc/{pid}/ns/mnt").st_ino != os.stat("/proc/self/ns/mnt").st_ino
        visible = set(os.listdir(f"/proc/{pid}/root"))
        cgroup = Path(f"/proc/{pid}/cgroup").read_text()
        out = await run
        self.assertEqual(out, "exit=0\ndone\n")
        self.assertEqual(status["Uid"].split()[:2], ["65534", "65534"])
        self.assertEqual(status["Gid"].split()[:2], ["65534", "65534"])
        self.assertEqual(int(status["CapEff"], 16), 0)
        self.assertTrue(own_mounts)
        self.assertIn("vmlinux", visible)
        self.assertFalse(visible & {"etc", "home", "usr", "proc"}, visible)  # none of the host's files
        jail_id = re.search(r"firecracker/(aios-mvm-[0-9a-f]{16})", cgroup)
        self.assertIsNotNone(jail_id, cgroup)
        jail = aios.MicroVMSandbox.paths()["jails"] / "firecracker" / jail_id[1]
        self.assertFalse(jail.exists())  # removed after the call
        self.assertFalse(Path(f"/sys/fs/cgroup/firecracker/{jail_id[1]}").exists())
        self.assertIsNone(self.vmm())

    async def test_the_guest_sandbox_still_holds(self):
        out = await self.box.run_python("import os, socket; print(os.getuid(), sorted(os.listdir('/sys/class/net')))")
        self.assertEqual(out, "exit=0\n65534 ['lo']\n")


if __name__ == "__main__":
    unittest.main()
