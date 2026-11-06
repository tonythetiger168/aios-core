"""Memory admission control for run_python sandboxes (Docker containers, Firecracker microVMs).

A swarm of a thousand helpers can ask for a thousand sandboxes at once. Each one is capped (256 MiB
by default), but the caps add up: started all together they can push the host into swap or its
OOM killer, which may pick AIOS itself. The gate starts a sandbox only while the memory it may use
fits in what the host has free; the others wait their turn (in arrival order) instead of failing.
The idea follows the agent-sandbox schedulers of TenonAgent (arXiv 2611.00051) and ToolSched
(arXiv 2611.05269), reduced to what the standard library can see.

It is conservative: a running sandbox counts at its full limit, although the host's free memory
already shows what it really uses. One sandbox is always admitted when none is running, so a
small machine still runs code one call at a time. Free memory comes from /proc/meminfo
(MemAvailable) on Linux and GlobalMemoryStatusEx on Windows; elsewhere (macOS) only the optional
count cap applies.

    AIOS_SANDBOX_RESERVE_MB   memory left for everything else (default 512)
    AIOS_SANDBOX_MAX          at most this many sandboxes at once (default: no count cap)
    AIOS_SANDBOX_WAIT         seconds a call may wait for room before it fails (default 300)
"""
import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager

RESERVE_MB = 512
WAIT_S = 300.0
POLL_S = 0.2


def available_mb() -> int | None:
    """Memory the host can give new processes without swapping, in MiB; None when unknown."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo", encoding="ascii") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) // 1024
        except (OSError, ValueError, IndexError):
            return None
        return None
    if sys.platform == "win32":
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MemoryStatusEx()
        st.dwLength = ctypes.sizeof(MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullAvailPhys // (1 << 20)
    return None


def _env_int(name: str, default):
    v = os.environ.get(name, "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, not {v!r}")


class AdmissionError(RuntimeError):
    pass


class MemoryGate:
    """Admits sandboxes while their memory limits fit; one gate per process (see GATE)."""

    def __init__(self, reserve_mb: int | None = None, max_running: int | None = None,
                 wait_s: float | None = None, probe=available_mb):
        self.reserve_mb = _env_int("AIOS_SANDBOX_RESERVE_MB", RESERVE_MB) if reserve_mb is None else reserve_mb
        self.max_running = _env_int("AIOS_SANDBOX_MAX", None) if max_running is None else max_running
        self.wait_s = float(_env_int("AIOS_SANDBOX_WAIT", WAIT_S)) if wait_s is None else wait_s
        self.probe = probe
        self.running = 0  # sandboxes admitted and not finished
        self.reserved_mb = 0  # their limits, added up
        self.waited = 0  # calls that had to wait at least once (for doctor / tests)
        self._turn = None  # asyncio.Lock, made in the running loop: waiters go in arrival order

    def fits(self, need_mb: int) -> bool:
        if self.running == 0:
            return True  # never block the only call: a small machine runs one at a time
        if self.max_running is not None and self.running >= self.max_running:
            return False
        free = self.probe()
        return free is None or free - self.reserved_mb >= need_mb + self.reserve_mb

    def capacity(self, need_mb: int) -> int | None:
        """How many sandboxes of need_mb the host fits right now (None: free memory unknown)."""
        free = self.probe()
        n = None if free is None else max(1, (free - self.reserve_mb) // max(need_mb, 1))
        if self.max_running is not None:
            n = self.max_running if n is None else min(n, self.max_running)
        return n

    @asynccontextmanager
    async def admit(self, need_mb: int):
        loop = asyncio.get_running_loop()
        if self._turn is None or getattr(self._turn, "_aios_loop", None) is not loop:
            self._turn = asyncio.Lock()
            self._turn._aios_loop = loop
        async with self._turn:  # one waiter at a time checks, so a big call is not starved
            deadline, waited = time.monotonic() + self.wait_s, False
            while not self.fits(need_mb):
                if time.monotonic() >= deadline:
                    raise AdmissionError(
                        f"not started: no room for another {need_mb} MiB sandbox after {self.wait_s:g}s "
                        f"({self.running} running, {self.probe()} MiB free, {self.reserve_mb} MiB kept in "
                        "reserve; see AIOS_SANDBOX_RESERVE_MB / AIOS_SANDBOX_MAX)")
                if not waited:
                    self.waited, waited = self.waited + 1, True
                await asyncio.sleep(POLL_S)
            self.running += 1
            self.reserved_mb += need_mb
        try:
            yield
        finally:
            self.running -= 1
            self.reserved_mb -= need_mb


GATE: MemoryGate | None = None


def gate() -> MemoryGate:
    global GATE
    if GATE is None:
        GATE = MemoryGate()
    return GATE
