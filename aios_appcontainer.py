"""Windows: run a program in an AppContainer inside a job object (the `process` sandbox's confinement
on Windows, as Landlock is on Linux and sandbox-exec on macOS). Standard library only (ctypes).

An AppContainer process runs at low integrity with a token of its own, so it can open only what
grants that container (or "ALL APPLICATION PACKAGES") access: Windows itself and Program Files can be
read; your user folder, other drives' data and the registry under your user cannot. With no
capabilities it has no network at all (loopback included). AIOS grants its container read access to
Python's own folder and the workspace, and full access only to the run's throwaway folder; the
grant on Python's folder stays (undo: `icacls <folder> /remove *<SID>`, the SID `aios doctor` prints).
The job object caps memory per process, CPU time, how many processes may exist at once, and kills
every one of them when the run ends or times out.
"""
import ctypes
import functools
import os
import subprocess
import sys
import threading
from ctypes import wintypes

NAME = "aios.sandbox"  # the AppContainer profile (one per user, made on first use)

if sys.platform == "win32":
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)

    class SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.c_void_p),
                    ("CapabilityCount", wintypes.DWORD), ("Reserved", wintypes.DWORD)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                      "OtherOperationCount", "ReadTransferCount",
                                                      "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.CreateAppContainerProfile.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                                  ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                      ctypes.POINTER(ctypes.c_size_t)]
    k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                              ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                   wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                   ctypes.c_void_p, ctypes.c_void_p]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.ResumeThread.argtypes = [wintypes.HANDLE]
    k32.ResumeThread.restype = wintypes.DWORD
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

PROC_THREAD_ATTRIBUTE_HANDLE_LIST, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020002, 0x00020009
EXTENDED_STARTUPINFO_PRESENT, CREATE_SUSPENDED = 0x00080000, 0x00000004
CREATE_UNICODE_ENVIRONMENT, CREATE_NO_WINDOW = 0x00000400, 0x08000000
STARTF_USESTDHANDLES = 0x00000100
JOB_OBJECT_LIMIT_PROCESS_TIME, JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x2, 0x8
JOB_OBJECT_LIMIT_PROCESS_MEMORY, JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x100, 0x400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
ALREADY_EXISTS = -2147024713  # HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS) as a signed long
WAIT_TIMEOUT = 0x102
TIMED_OUT = -1  # run()'s exit code when it was stopped for taking too long


def _fail(what: str):
    err = ctypes.get_last_error()
    raise OSError(f"{what} failed: {ctypes.FormatError(err)} (error {err})")


@functools.lru_cache(maxsize=1)
def container_sid() -> tuple:
    """(SID pointer, SID string) of the AIOS AppContainer, created on first use."""
    sid = ctypes.c_void_p()
    hr = userenv.CreateAppContainerProfile(NAME, "AIOS sandbox", "Runs run_python code for AIOS", None, 0,
                                           ctypes.byref(sid))
    if hr == ALREADY_EXISTS:
        hr = userenv.DeriveAppContainerSidFromAppContainerName(NAME, ctypes.byref(sid))
    if hr != 0:
        raise OSError(f"cannot create the AppContainer profile (HRESULT {hr & 0xFFFFFFFF:#010x})")
    text = wintypes.LPWSTR()
    if not adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        _fail("ConvertSidToStringSidW")
    s = text.value
    k32.LocalFree(text)
    return sid, s  # kept for the life of the process


@functools.lru_cache(maxsize=1)
def unavailable() -> str | None:
    """Why an AppContainer cannot be used here, or None."""
    if sys.platform != "win32":
        return "AppContainers exist only on Windows"
    try:
        container_sid()
    except OSError as e:
        return str(e)
    return None


_granted: set = set()
_grant_lock = threading.Lock()


def grant(path: str, full: bool = False) -> None:
    """Let the container read (or, with full, change) path and everything under it. Done once per
    folder and process; icacls skips an ACE that is already there."""
    key = (os.path.normcase(os.path.abspath(path)), full)
    with _grant_lock:
        if key in _granted:
            return
        r = subprocess.run(["icacls", key[0], "/grant", f"*{container_sid()[1]}:(OI)(CI){'F' if full else 'RX'}",
                            "/Q"], capture_output=True, text=True)
        if r.returncode:
            raise OSError(f"icacls could not grant the sandbox access to {path}: {(r.stdout + r.stderr).strip()}")
        _granted.add(key)


def python_dirs() -> list[str]:
    """Folders the sandboxed Python must be able to read: its own install (and a venv's, if any)."""
    exe = getattr(sys, "_base_executable", None) or sys.executable
    dirs = {sys.base_prefix, sys.base_exec_prefix, os.path.dirname(os.path.abspath(exe))}
    return sorted(d for d in dirs if d and os.path.isdir(d))


def base_python() -> str:
    """The real python.exe (a venv's python.exe is a launcher that starts it as a child)."""
    exe = getattr(sys, "_base_executable", None) or sys.executable
    return exe if os.path.isfile(exe) else sys.executable


def run(argv: list[str], cwd: str, env: dict, timeout: float, cpu_s: int, mem: int, max_procs: int,
        max_out: int) -> tuple[int, bytes]:
    """Run argv in the container and a job; (exit code or TIMED_OUT, the first max_out bytes of
    stdout and stderr together). Blocking: call it in a thread."""
    sid = container_sid()[0]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        _fail("CreateJobObjectW")
    attrs = None
    proc = PROCESS_INFORMATION()
    rd = wr = nul = None
    try:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        b = info.BasicLimitInformation
        b.LimitFlags = (JOB_OBJECT_LIMIT_PROCESS_TIME | JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_PROCESS_MEMORY
                        | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        b.PerProcessUserTimeLimit = int(cpu_s * 10_000_000)  # in 100 ns
        b.ActiveProcessLimit = max_procs
        info.ProcessMemoryLimit = mem
        if not k32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info),
                                           ctypes.sizeof(info)):
            _fail("SetInformationJobObject")

        import msvcrt
        rd, wr = os.pipe()
        nul = os.open(os.devnull, os.O_RDONLY)
        for fd in (wr, nul):
            os.set_inheritable(fd, True)
        # the child inherits exactly these two handles, not others another thread made inheritable
        inherit = (wintypes.HANDLE * 2)(msvcrt.get_osfhandle(nul), msvcrt.get_osfhandle(wr))
        caps = SECURITY_CAPABILITIES(sid, None, 0, 0)  # no capabilities: no network, no libraries, ...
        size = ctypes.c_size_t()
        k32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        if not k32.InitializeProcThreadAttributeList(attrs, 2, 0, ctypes.byref(size)):
            attrs = None
            _fail("InitializeProcThreadAttributeList")
        for attr, value in ((PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, caps),
                            (PROC_THREAD_ATTRIBUTE_HANDLE_LIST, inherit)):
            if not k32.UpdateProcThreadAttribute(attrs, 0, attr, ctypes.byref(value), ctypes.sizeof(value),
                                                 None, None):
                _fail("UpdateProcThreadAttribute")
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(si)
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput = inherit[0], inherit[1]
        si.StartupInfo.hStdError = inherit[1]
        si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
        block = ctypes.create_unicode_buffer("".join(f"{k}={v}\0" for k, v in env.items()) + "\0")
        cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW
        if not k32.CreateProcessW(argv[0], cmdline, None, None, True, flags, block, cwd, ctypes.byref(si),
                                  ctypes.byref(proc)):
            _fail("CreateProcessW")
        os.close(wr)
        os.close(nul)
        wr = nul = None
        if not k32.AssignProcessToJobObject(job, proc.hProcess):
            k32.TerminateProcess(proc.hProcess, 1)
            _fail("AssignProcessToJobObject")
        k32.ResumeThread(proc.hThread)

        out = bytearray()

        def drain():
            while True:
                try:
                    chunk = os.read(rd, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                if len(out) < max_out:
                    out.extend(chunk[:max_out - len(out)])

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        if k32.WaitForSingleObject(proc.hProcess, int(timeout * 1000)) == WAIT_TIMEOUT:
            code = TIMED_OUT
        else:
            ec = wintypes.DWORD()
            k32.GetExitCodeProcess(proc.hProcess, ctypes.byref(ec))
            code = ec.value if ec.value < 0x80000000 else ec.value - 0x100000000
        k32.TerminateJobObject(job, 1)  # whatever it started goes too; then the pipe reaches EOF
        reader.join(5)
        return code, bytes(out)
    finally:
        for fd in (wr, nul, rd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        for h in (proc.hThread, proc.hProcess):
            if h:
                k32.CloseHandle(h)
        if attrs is not None:
            k32.DeleteProcThreadAttributeList(attrs)
        k32.CloseHandle(job)
