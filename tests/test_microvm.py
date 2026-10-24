"""microVM sandbox: the host side against a fake firecracker, the guest init's job handling,
and (with AIOS_TEST_MICROVM=1) the real thing: Firecracker on KVM. See docs/microvm-sandbox.md."""
import asyncio
import json
import os
import platform
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
        json.dump({{"args": args, "config": cfg, "cwd": os.getcwd()}}, f)
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


def make_home(tmp: Path, fc=True, kernel=True, rootfs=True) -> Path:
    if fc:
        p = tmp / "firecracker"
        p.write_text(FAKE_FC.format(python=sys.executable, repo=str(REPO)))
        p.chmod(0o755)
    if kernel:
        (tmp / "vmlinux").write_bytes(b"kernel")
    if rootfs:
        (tmp / "rootfs.ext4").write_bytes(b"rootfs")
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
        with tempfile.TemporaryDirectory() as d, \
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
        return aios.MicroVMSandbox(home=self.home, **kw)

    def workdirs(self):
        return sorted(Path(tempfile.gettempdir()).glob("aios-mvm-*"))

    async def test_runs_code_and_cleans_up(self):
        before = self.workdirs()
        out = await self.box().run_python("import sys; print(6*7); sys.exit(3)")
        self.assertEqual(out, "exit=3\n42\n")
        self.assertEqual(self.workdirs(), before)  # job image and config removed

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

    async def test_fork_bomb_and_output_flood_are_contained(self):
        out = await aios.MicroVMSandbox(timeout=5, max_out=1000).run_python(
            "import os\nprint('y' * 50000, flush=True)\n"
            "for _ in range(200):\n"
            "    try: os.fork()\n"
            "    except OSError: break\n")
        self.assertTrue(out.startswith(("exit=", "killed: timeout")), out)
        self.assertIn("...(truncated)", out)


if __name__ == "__main__":
    unittest.main()
