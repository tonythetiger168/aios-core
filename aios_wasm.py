"""run_python inside WebAssembly: CPython compiled to WASI, run by wasmtime (`--sandbox wasm`).

    pip install wasmtime==50.0.1                       # the only extra; AIOS itself stays stdlib-only
    curl -fsSLo python.wasm https://github.com/vmware-labs/webassembly-language-runtimes/releases/download/python%2F3.12.0%2B20231211-040d5a6/python-3.12.0.wasm
    export AIOS_WASM_PYTHON=$PWD/python.wasm           # checked against PYTHON_WASM_SHA256 before every use
    python aios.py chat --sandbox wasm ...

What the code inside can do, by construction rather than by OS features (works the same on Linux,
macOS and Windows):
- no network: WASI preview 1 has no sockets at all;
- files: the workspace read-only at /workspace and a fresh, empty, writable /tmp; nothing else of the
  host exists for it;
- CPU: a fuel budget (wasmtime counts instructions) plus a wall-clock timeout that kills the host
  process running it; memory: a cap on the WebAssembly linear memory;
- no subprocesses, no threads, no native extensions (only pure-Python stdlib modules).
The interpreter file is pinned by SHA-256: a different python.wasm is refused unless you set
AIOS_WASM_PYTHON_SHA256 to its hash yourself. Verified only by the `wasm` CI job.
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

PYTHON_WASM_URL = ("https://github.com/vmware-labs/webassembly-language-runtimes/releases/download/"
                   "python%2F3.12.0%2B20231211-040d5a6/python-3.12.0.wasm")
PYTHON_WASM_SHA256 = "e5dc5a398b07b54ea8fdb503bf68fb583d533f10ec3f930963e02b9505f7a763"
ENV_PATH, ENV_SHA = "AIOS_WASM_PYTHON", "AIOS_WASM_PYTHON_SHA256"

# Runs in a separate host process (so a timeout can kill it): reads a JSON job on stdin.
RUNNER = r"""
import json, sys
job = json.load(sys.stdin)
import wasmtime as w
cfg = w.Config()
cfg.consume_fuel = True
eng = w.Engine(cfg)
mod = w.Module.from_file(eng, job["wasm"])
linker = w.Linker(eng)
linker.define_wasi()
store = w.Store(eng)
store.set_fuel(job["fuel"])
store.set_limits(memory_size=job["mem"])
wc = w.WasiConfig()
wc.argv = ["python", "-I", "-c", job["code"]]
wc.env = [["TMPDIR", "/tmp"], ["HOME", "/tmp"], ["PYTHONDONTWRITEBYTECODE", "1"]]
wc.stdout_file = job["out"]
wc.stderr_file = job["out"]
wc.preopen_dir(job["workspace"], "/workspace", w.DirPerms.READ_ONLY, w.FilePerms.READ_ONLY)
wc.preopen_dir(job["tmp"], "/tmp", w.DirPerms.READ_WRITE, w.FilePerms.READ_WRITE)
store.set_wasi(wc)
code = 0
try:
    linker.instantiate(store, mod).exports(store)["_start"](store)
except w.ExitTrap as e:
    code = e.code
except w.Trap as e:
    msg = str(e).splitlines()[0] if str(e) else "trap"
    code = 137 if "fuel" in msg else 134
    with open(job["out"], "a") as f:
        f.write("\n[wasm] stopped: " + ("CPU budget (fuel) used up" if "fuel" in msg else msg))
except w.WasmtimeError as e:
    code = 134
    with open(job["out"], "a") as f:
        f.write("\n[wasm] error: " + str(e).splitlines()[0])
sys.exit(code if 0 <= code < 256 else 1)
"""


def interpreter() -> str | None:
    p = os.environ.get(ENV_PATH, "").strip()
    return p or None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unavailable() -> str | None:
    """Why --sandbox wasm cannot run here, or None."""
    try:
        import wasmtime  # noqa: F401
    except ImportError:
        return "needs the wasmtime package: pip install wasmtime"
    path = interpreter()
    if not path:
        return f"set {ENV_PATH} to a WASI build of CPython (see aios_wasm: {PYTHON_WASM_URL})"
    if not Path(path).is_file():
        return f"{ENV_PATH}={path}: no such file"
    want = os.environ.get(ENV_SHA, "").strip().lower() or PYTHON_WASM_SHA256
    got = sha256_file(path)
    if got != want:
        return (f"{ENV_PATH}={path} has sha256 {got}, expected {want} (a different interpreter: set "
                f"{ENV_SHA} to its hash if you trust it)")
    return None


class WasmSandbox:
    name = "wasm"
    net_isolated = "all"
    fs_isolated = "wasm"

    def __init__(self, workspace, timeout=10, mem_mb=256, fuel=20_000_000_000, max_out=10_000, wasm=None):
        self._workspace, self.timeout, self.mem_mb = workspace, timeout, mem_mb
        self.fuel, self.max_out = fuel, max_out
        self.wasm = wasm or interpreter()

    @property
    def workspace(self) -> Path:
        return Path(self._workspace() if callable(self._workspace) else self._workspace)

    def job(self, code: str, out: str, tmp: str) -> dict:
        return {"wasm": self.wasm, "code": code, "out": out, "tmp": tmp, "workspace": str(self.workspace),
                "fuel": self.fuel, "mem": self.mem_mb * 1024 * 1024}

    async def run_python(self, code: str) -> str:
        self.workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as d:
            out, tmp = os.path.join(d, "out.txt"), os.path.join(d, "tmp")
            os.mkdir(tmp)
            Path(out).touch()
            env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "HOME") if k in os.environ}  # no secrets
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", RUNNER, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env)
            try:
                _, err = await asyncio.wait_for(proc.communicate(json.dumps(self.job(code, out, tmp)).encode()),
                                                self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"killed: timeout after {self.timeout}s"
            text = Path(out).read_text(encoding="utf-8", errors="replace")
            if proc.returncode not in (0, 1) and err:  # the host side failed (bad module, wasmtime error)
                text += "\n[wasm host] " + err.decode(errors="replace").strip().splitlines()[-1]
            if len(text) > self.max_out:
                text = text[:self.max_out] + f"\n... ({len(text) - self.max_out} more characters cut)"
            return f"exit={proc.returncode}\n{text}"
