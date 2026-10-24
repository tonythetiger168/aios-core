#!/usr/local/bin/python3 -IB
"""Guest side of the AIOS microVM sandbox (see docs/microvm-sandbox.md).

Installed as /aios-init in the guest root filesystem and started by the kernel as PID 1.
It reads one job from the second (read-only) drive, runs the code once as an unprivileged
user with a timeout, prints the bounded result between nonce markers on the serial console
and powers the VM off. Standard library only; runs on the guest's Python, never the host's.

Job drive layout: "AIOS1 <nonce> <timeout> <max_out> <code_len>\n" + code (UTF-8),
zero-padded to a 512-byte multiple.
"""
import base64
import ctypes
import os
import subprocess
import sys
try:
    import resource
except ImportError:  # not POSIX: only parse_job/result_lines are usable (tests)
    resource = None

MAGIC = "AIOS1"
JOB_DEVICE = "/dev/vdb"
HEADER_MAX = 256
CODE_MAX = 1 << 20  # 1 MiB of source is plenty for a tool call
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
        with open(JOB_DEVICE, "rb") as f:
            nonce, timeout, max_out, code = parse_job(f.read(HEADER_MAX + CODE_MAX))
        status, rc, out, truncated = run_code(code, timeout, max_out, "/tmp", "/run")
        console.write(result_lines(nonce, status, rc, out, truncated))
    except Exception as e:  # the host reports a missing result as a sandbox failure
        console.write(f"\nAIOS-INIT-ERROR {type(e).__name__}: {str(e)[:200]}\n")
    console.flush()
    _power_off()


if __name__ == "__main__" and os.getpid() == 1:
    main()
