#!/usr/local/bin/python3 -IB
"""Guest side of the AIOS microVM sandbox (see docs/microvm-sandbox.md).

Installed as /aios-init in the guest root filesystem and started by the kernel as PID 1.
It reads one job from the second (read-only) drive, runs the code once as an unprivileged
user with a timeout, prints the bounded result between nonce markers on the serial console
and powers the VM off. Standard library only; runs on the guest's Python, never the host's.

Job drive layout: "AIOS1 <nonce> <timeout> <max_out> <code_len>\n" + code (UTF-8), then
"\nAIOSX seed=<64 hex> time=<unix time>\n", zero-padded to a 512-byte multiple (to a fixed
size for snapshots). An older init ignores the AIOSX line.

Snapshot mode (`aios_wait=1` on the kernel command line): after mounting, the init prints
AIOS-READY and waits for a line on the console. The host snapshots the VM there, once, and
every call restores that snapshot with its own job drive and sends the line. Each restored VM
starts from the same memory, so before reading the job the init drops the block cache, and it
mixes the job's seed into the kernel's random pool and forces a reseed, so no two calls share
random numbers; it also sets the clock, which stopped at snapshot time.
"""
import base64
import ctypes
import os
import struct
import subprocess
import sys
import time
try:
    import fcntl
except ImportError:  # not POSIX (tests)
    fcntl = None
try:
    import resource
except ImportError:  # not POSIX: only parse_job/result_lines are usable (tests)
    resource = None

MAGIC = "AIOS1"
JOB_DEVICE = "/dev/vdb"
HEADER_MAX = 256
CODE_MAX = 1 << 20  # 1 MiB of source is plenty for a tool call
EXTRA_MAX = 256
WAIT_FLAG = "aios_wait=1"
READY = "AIOS-READY"
BLKFLSBUF = 0x1261
RNDADDENTROPY = 0x40085203
RNDRESEEDCRNG = 0x5207
UNPRIVILEGED = 65534  # nobody / nogroup
CHILD_FSIZE = 8 << 20  # largest file the code may write (output file included)
CHILD_NPROC = 64


class JobError(ValueError):
    pass


def parse_job(data: bytes):
    """-> (nonce, timeout, max_out, code). Raises JobError on anything malformed."""
    head, sep, rest = data[:HEADER_MAX].partition(b"\n")
    if not sep:
        raise JobError("job header missing")
    parts = head.decode("ascii", "replace").split()
    if len(parts) != 5 or parts[0] != MAGIC:
        raise JobError("bad job header")
    nonce = parts[1]
    if not (16 <= len(nonce) <= 64 and all(c in "0123456789abcdef" for c in nonce)):
        raise JobError("bad nonce")
    try:
        timeout, max_out, length = (int(p) for p in parts[2:])
    except ValueError:
        raise JobError("bad job numbers") from None
    if not (0 < timeout <= 3600 and 0 < max_out <= CHILD_FSIZE and 0 <= length <= CODE_MAX):
        raise JobError("job numbers out of range")
    body = data[len(head) + 1:]
    if len(body) < length:
        raise JobError("job truncated")
    return nonce, timeout, max_out, body[:length]


def parse_extras(data: bytes) -> dict:
    """The AIOSX line after the code: {'seed': bytes, 'time': float}, or {} (older host)."""
    head, _, body = data[:HEADER_MAX + CODE_MAX + EXTRA_MAX].partition(b"\n")
    try:
        length = int(head.split()[4])
    except (IndexError, ValueError):
        return {}
    line = body[length:length + EXTRA_MAX]
    if not line.startswith(b"\nAIOSX "):
        return {}
    fields = dict(f.split("=", 1) for f in line[7:].split(b"\n", 1)[0].decode("ascii", "replace").split()
                  if "=" in f)
    out = {}
    try:
        seed = bytes.fromhex(fields.get("seed", ""))
        if len(seed) == 32:
            out["seed"] = seed
        if "time" in fields:
            out["time"] = float(fields["time"])
    except ValueError:
        pass
    return out


def reseed(seed: bytes):
    """Mix seed into the kernel's input pool and reseed its generator right away (as root)."""
    fd = os.open("/dev/urandom", os.O_WRONLY)
    try:
        fcntl.ioctl(fd, RNDADDENTROPY, struct.pack("ii", len(seed) * 8, len(seed)) + seed)
        fcntl.ioctl(fd, RNDRESEEDCRNG)
    finally:
        os.close(fd)


def read_job(path: str, flush: bool) -> bytes:
    with open(path, "rb") as f:
        if flush:  # the kernel read this device at boot, before the snapshot: drop that cache
            fcntl.ioctl(f.fileno(), BLKFLSBUF)
        return f.read(HEADER_MAX + CODE_MAX + EXTRA_MAX)


def wait_for_go(console):
    console.write(f"\n{READY}\n")
    console.flush()
    while True:
        chunk = os.read(0, 64)
        if not chunk:
            raise JobError("console closed before the job was sent")
        if b"\n" in chunk:
            return


def result_lines(nonce: str, status: str, rc: int, out: bytes, truncated: bool) -> str:
    """The console block the host looks for. Only the init writes the console, and the code
    never sees the nonce, so the code cannot forge a block."""
    enc = base64.b64encode(out).decode()
    body = "\n".join(enc[i:i + 76] for i in range(0, len(enc), 76))
    return (f"\nAIOS-BEGIN-{nonce}\n{body}\n"
            f"AIOS-END-{nonce} status={status} rc={rc} truncated={int(truncated)}\n")


def _drop_privileges():
    """Runs in the child between fork and exec."""
    os.setgroups([])
    os.setresgid(UNPRIVILEGED, UNPRIVILEGED, UNPRIVILEGED)
    os.setresuid(UNPRIVILEGED, UNPRIVILEGED, UNPRIVILEGED)
    resource.setrlimit(resource.RLIMIT_FSIZE, (CHILD_FSIZE, CHILD_FSIZE))
    resource.setrlimit(resource.RLIMIT_NPROC, (CHILD_NPROC, CHILD_NPROC))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        os._exit(126)


def run_code(code: bytes, timeout: int, max_out: int, workdir: str, outdir: str,
             python=sys.executable, unprivileged=True):
    """Run the code once. -> (status, rc, output, truncated); status is ok or timeout."""
    code_path = os.path.join(outdir, "code.py")
    out_path = os.path.join(outdir, "out")
    with open(code_path, "wb") as f:
        f.write(code)
    with open(code_path, "rb") as stdin, open(out_path, "wb") as out:
        env = {"HOME": workdir, "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
        proc = subprocess.Popen([python, "-I", "-B", "-X", "utf8", "-"], stdin=stdin,
                                stdout=out, stderr=subprocess.STDOUT, cwd=workdir, env=env,
                                start_new_session=True,
                                preexec_fn=_drop_privileges if unprivileged else None)
        status = "ok"
        try:
            rc = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            status = "timeout"
        try:
            os.killpg(proc.pid, 9)  # leftovers too: the session is the code's whole tree
        except (ProcessLookupError, PermissionError):
            pass
        rc = proc.wait()
    with open(out_path, "rb") as f:
        data = f.read(max_out + 1)
    return status, rc, data[:max_out], len(data) > max_out


def _mount(*args) -> bool:
    return subprocess.run(["/bin/mount", *args], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, check=False).returncode == 0


def _power_off():
    os.sync()
    ctypes.CDLL(None).reboot(0x01234567)  # RB_AUTOBOOT; with reboot=k the VMM exits
    os._exit(0)  # PID 1 exiting panics the guest, and panic=1 also ends the VM


def main():
    console = sys.stdout
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("-1000")
    except OSError:
        pass
    try:
        if not _mount("-t", "proc", "-o", "nosuid,nodev,noexec,hidepid=invisible", "proc", "/proc"):
            _mount("-t", "proc", "-o", "nosuid,nodev,noexec,hidepid=2", "proc", "/proc")
        _mount("-t", "sysfs", "-o", "nosuid,nodev,noexec", "sysfs", "/sys")
        _mount("-t", "devtmpfs", "-o", "nosuid,noexec", "devtmpfs", "/dev")
        _mount("-t", "tmpfs", "-o", "nosuid,nodev,mode=1777,size=16m", "tmpfs", "/tmp")
        _mount("-t", "tmpfs", "-o", "nosuid,nodev,noexec,mode=700,size=24m", "tmpfs", "/run")
        with open("/proc/cmdline") as f:
            restored = WAIT_FLAG in f.read().split()
        if restored:
            wait_for_go(console)  # the snapshot is taken here; each call resumes from here
        data = read_job(JOB_DEVICE, flush=restored)
        nonce, timeout, max_out, code = parse_job(data)
        extras = parse_extras(data)
        if restored:  # a clone must not run with its snapshot's random state
            if "seed" not in extras:
                raise JobError("a restored VM needs a fresh seed")
            reseed(extras["seed"])
        elif "seed" in extras:
            try:
                reseed(extras["seed"])
            except OSError:
                pass
        if restored and "time" in extras:  # the clock stopped when the snapshot was taken
            try:
                time.clock_settime(time.CLOCK_REALTIME, extras["time"])
            except (OSError, AttributeError):
                pass
        status, rc, out, truncated = run_code(code, timeout, max_out, "/tmp", "/run")
        console.write(result_lines(nonce, status, rc, out, truncated))
    except Exception as e:  # the host reports a missing result as a sandbox failure
        console.write(f"\nAIOS-INIT-ERROR {type(e).__name__}: {str(e)[:200]}\n")
    console.flush()
    _power_off()


if __name__ == "__main__" and os.getpid() == 1:
    main()
