"""--sandbox wasm (aios_wasm). The real-interpreter tests run only with AIOS_TEST_WASM=1 (the `wasm` CI job)."""
import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_wasm  # noqa: E402

REAL = os.environ.get("AIOS_TEST_WASM") == "1"


class SetupTests(unittest.TestCase):
    def test_reasons_it_cannot_run(self):
        fake = types.ModuleType("wasmtime")
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(sys.modules, {"wasmtime": fake}):
            with mock.patch.dict(os.environ, {aios_wasm.ENV_PATH: ""}):
                self.assertIn(aios_wasm.ENV_PATH, aios_wasm.unavailable())
            with mock.patch.dict(os.environ, {aios_wasm.ENV_PATH: str(Path(d, "nope.wasm"))}):
                self.assertIn("no such file", aios_wasm.unavailable())
            other = Path(d, "python.wasm")
            other.write_bytes(b"\0asm not the pinned interpreter")
            with mock.patch.dict(os.environ, {aios_wasm.ENV_PATH: str(other), aios_wasm.ENV_SHA: ""}):
                why = aios_wasm.unavailable()
                self.assertIn("expected " + aios_wasm.PYTHON_WASM_SHA256, why)
            with mock.patch.dict(os.environ, {aios_wasm.ENV_PATH: str(other),
                                              aios_wasm.ENV_SHA: aios_wasm.sha256_file(str(other))}):
                self.assertIsNone(aios_wasm.unavailable())  # the person pinned this one themselves

    def test_without_wasmtime(self):
        with mock.patch.dict(sys.modules, {"wasmtime": None}):
            self.assertIn("pip install wasmtime", aios_wasm.unavailable())

    def test_asked_for_isolation_never_degrades(self):
        with mock.patch.object(aios_wasm, "unavailable", return_value="no wasmtime"):
            with self.assertRaises(RuntimeError) as e:
                aios.make_sandbox("wasm")
            self.assertIn("--sandbox wasm", str(e.exception))

    def test_job_mounts_workspace_read_only_and_reads_it_late(self):
        ws = {"p": "/first"}
        box = aios_wasm.WasmSandbox(lambda: ws["p"], wasm="/x/python.wasm")
        ws["p"] = "/later"
        job = box.job("print(1)", "/o", "/t")
        self.assertEqual((job["workspace"], job["tmp"], job["wasm"]), (str(Path("/later")), "/t", "/x/python.wasm"))
        self.assertIn("READ_ONLY, w.FilePerms.READ_ONLY", aios_wasm.RUNNER)
        self.assertIn('"/workspace"', aios_wasm.RUNNER)

    def test_tool_description_names_the_backend(self):
        box = aios_wasm.WasmSandbox("/w", wasm="/x")
        k = aios.Kernel(store=aios.Store(":memory:"))
        with mock.patch.object(aios, "make_sandbox", return_value=box):
            aios.install_builtin_tools(k)
        desc = next(t["description"] for t in k.tools.schemas_for({"run_python"}))
        self.assertIn("WebAssembly", desc)
        self.assertIn("no network", desc)


@unittest.skipUnless(REAL, "set AIOS_TEST_WASM=1 with wasmtime and AIOS_WASM_PYTHON (the `wasm` CI job)")
class RealWasmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        why = aios_wasm.unavailable()
        if why:
            raise AssertionError(f"AIOS_TEST_WASM=1 but: {why}")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "data.txt").write_text("hello from the workspace", encoding="utf-8")
        self.box = aios_wasm.WasmSandbox(self.ws, timeout=60)

    def tearDown(self):
        self._tmp.cleanup()

    def run_code(self, code, **kw):
        box = aios_wasm.WasmSandbox(self.ws, **{"timeout": 60, **kw}) if kw else self.box
        out = asyncio.run(box.run_python(code))
        print(f"\n--- {code[:60]!r}\n{out[:400]}")  # evidence in the CI log
        return out

    def test_runs_python_and_reads_the_workspace(self):
        out = self.run_code("import sys; print(sys.platform, sum(range(10)));"
                            "print(open('/workspace/data.txt').read())")
        self.assertTrue(out.startswith("exit=0"), out)
        self.assertIn("wasi 45", out)
        self.assertIn("hello from the workspace", out)

    def test_workspace_is_read_only_and_tmp_is_writable(self):
        out = self.run_code("open('/tmp/x.txt','w').write('ok'); print(open('/tmp/x.txt').read());"
                            "open('/workspace/new.txt','w').write('x')")
        self.assertIn("ok", out)
        self.assertNotIn("exit=0", out.splitlines()[0])
        self.assertFalse((self.ws / "new.txt").exists())

    def test_nothing_else_of_the_host_exists(self):
        out = self.run_code("import os\n"
                            "for p in ('/etc/passwd', '/home', '/root', '/proc/self/environ'):\n"
                            "    print(p, os.path.exists(p))\n"
                            "print('env', sorted(os.environ))")
        for p in ("/etc/passwd", "/home", "/root", "/proc/self/environ"):
            self.assertIn(f"{p} False", out)
        self.assertNotIn("GITHUB_TOKEN", out)
        self.assertNotIn("PATH'", out)

    def test_no_network(self):
        out = self.run_code("import socket\n"
                            "try:\n"
                            "    socket.create_connection(('1.1.1.1', 443), timeout=3); print('CONNECTED')\n"
                            "except Exception as e:\n"
                            "    print('refused', type(e).__name__)")
        self.assertNotIn("CONNECTED", out)

    def test_cpu_and_memory_budgets(self):
        out = self.run_code("while True: pass", fuel=2_000_000_000, timeout=120)
        self.assertIn("fuel", out)
        out = self.run_code("x = bytearray(1 << 30); print('ALLOCATED')", mem_mb=64)
        self.assertNotIn("ALLOCATED", out)
        out = self.run_code("while True: pass", fuel=10 ** 15, timeout=5)
        self.assertIn("killed: timeout after 5s", out)

    def test_as_the_run_python_tool(self):
        old = aios.WORKSPACE
        aios.WORKSPACE = self.ws
        try:
            k = aios.Kernel(store=aios.Store(":memory:"))
            aios.install_builtin_tools(k, "wasm")
            out = asyncio.run(k.tools._fns["run_python"](code="print(6*7)"))
        finally:
            aios.WORKSPACE = old
        print(f"\n--- tool: {str(out)[:200]}")
        self.assertIn("42", str(out))


if __name__ == "__main__":
    unittest.main()
