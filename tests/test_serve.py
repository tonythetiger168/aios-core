"""MCP server mode: the policy pipeline for an outside client, and the SDK glue.

The real `mcp` SDK is NOT required here: the glue is checked against a small stand-in that mimics
the 1.x low-level server API (Server, stdio_server, types). tests/test_mcp_interop.py drives the
real server with the SDK's own client when the SDK is installed (CI covers 1.x and 2.x).
"""
import asyncio
import contextlib
import io
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class WorkspaceCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = aios.WORKSPACE
        aios.WORKSPACE = Path(self._tmp.name).resolve()
        (aios.WORKSPACE / "doc.txt").write_text("hello from a file", encoding="utf-8")

    def tearDown(self):
        aios.WORKSPACE = self._old
        self._tmp.cleanup()

    def gateway(self, perms=None, scopes=None, name="mcp"):
        k = aios.Kernel(store=aios.Store(":memory:"))  # no approver, like `aios serve`
        aios.install_builtin_tools(k)
        agent = aios.Agent(name, set(perms if perms is not None else aios.SERVE_DEFAULT_TOOLS),
                           scopes=scopes or {})
        return aios.Gateway(k, agent)

    @staticmethod
    def log(gw):
        return gw.k.store.audit_tail()


class GatewayTests(WorkspaceCase):
    async def test_default_offer_is_read_only(self):
        names = {t["name"] for t in self.gateway().tools()}
        self.assertEqual(names, {"now", "read_file", "list_files", "recall", "list_facts"})

    async def test_read_is_tagged_audited_and_taints_the_session(self):
        gw = self.gateway()
        out, err = await gw.call("read_file", {"path": "doc.txt"})
        self.assertFalse(err)
        self.assertTrue(out.startswith('<untrusted source="read_file">'))
        self.assertTrue(gw.agent.session_tainted)
        (e,) = self.log(gw)
        self.assertEqual((e["agent"], e["tool"], e["status"]), ("mcp", "read_file", "ok"))
        self.assertEqual(e["result"], "hello from a file")  # audit keeps the raw text

    async def test_trusted_tools_do_not_taint(self):
        gw = self.gateway()
        out, err = await gw.call("now", {})
        self.assertFalse(err)
        self.assertNotIn("<untrusted", out)
        self.assertFalse(gw.agent.session_tainted)

    async def test_tools_outside_the_grant_are_refused(self):
        gw = self.gateway()
        for tool in ("write_file", "run_python", "remember", "no_such_tool"):
            with self.subTest(tool=tool):
                out, err = await gw.call(tool, {"path": "x", "content": "y", "code": "1", "key": "k", "value": "v"})
                self.assertTrue(err)
                self.assertIn("permission denied", out)
        self.assertFalse((aios.WORKSPACE / "x").exists())

    async def test_auto_args_pre_allow_values_over_stdio(self):
        gw = self.gateway(perms=["web__fetch"],
                          scopes={"web__fetch": {"auto_args": {"url": ["https://docs.python.org/*"]}}})
        fetched = []

        @gw.k.tools.register("web__fetch", "fetch", {"url": {"type": "string"}}, untrusted=True)
        async def fetch(url):
            fetched.append(url)
            return "page"

        gw.k.needs_approval.add("web__fetch")  # like every MCP tool
        out, err = await gw.call("web__fetch", {"url": "https://evil.example/"})
        self.assertTrue(err)
        self.assertIn("auto_args", out)  # the hint names the fix
        out, err = await gw.call("web__fetch", {"url": "https://docs.python.org/3/"})
        self.assertFalse(err)
        self.assertTrue(gw.agent.session_tainted)  # what it fetched is untrusted
        out, err = await gw.call("web__fetch", {"url": "https://docs.python.org/3/x"})
        self.assertTrue(err)  # tainted session: pre-allowed values no longer apply
        self.assertEqual(fetched, ["https://docs.python.org/3/"])

    async def test_risky_tool_without_prescope_is_refused_with_a_hint(self):
        gw = self.gateway(perms=["write_file"])
        out, err = await gw.call("write_file", {"path": "a.txt", "content": "x"})
        self.assertTrue(err)
        self.assertIn("not approved", out)
        self.assertIn("auto_paths", out)
        self.assertFalse((aios.WORKSPACE / "a.txt").exists())
        self.assertEqual(self.log(gw)[0]["status"], "denied")

    async def test_prescoped_path_works_in_a_clean_session_only(self):
        scopes = {"write_file": {"paths": ["out/*"], "auto_paths": ["out/*"]}}
        gw = self.gateway(perms=["write_file", "read_file"], scopes=scopes)
        out, err = await gw.call("write_file", {"path": "out/a.txt", "content": "ok"})
        self.assertFalse(err)
        self.assertEqual((aios.WORKSPACE / "out" / "a.txt").read_text(encoding="utf-8"), "ok")

        await gw.call("read_file", {"path": "doc.txt"})  # session is now tainted
        out, err = await gw.call("write_file", {"path": "out/b.txt", "content": "evil"})
        self.assertTrue(err)
        self.assertIn("untrusted", out)
        self.assertFalse((aios.WORKSPACE / "out" / "b.txt").exists())

    async def test_path_scope_still_applies(self):
        scopes = {"write_file": {"paths": ["out/*"], "auto_paths": ["out/*"]}}
        gw = self.gateway(perms=["write_file"], scopes=scopes)
        for bad in ("other.txt", "../escape.txt", "out/../other.txt"):
            with self.subTest(path=bad):
                out, err = await gw.call("write_file", {"path": bad, "content": "x"})
                self.assertTrue(err)
                self.assertIn("not allowed on path", out)

    async def test_memory_written_after_reading_is_gated(self):
        gw = self.gateway(perms=["remember", "read_file", "recall"])
        out, err = await gw.call("remember", {"key": "a", "value": "1"})  # clean: no approval needed
        self.assertFalse(err)
        self.assertEqual(gw.k.store.fact_info("mcp", "a")["source"], "model")
        await gw.call("read_file", {"path": "doc.txt"})
        out, err = await gw.call("remember", {"key": "b", "value": "2"})  # tainted: needs a person
        self.assertTrue(err)
        self.assertIsNone(gw.k.store.get_fact("mcp", "b"))

    async def test_max_calls_counts_per_session(self):
        gw = self.gateway(scopes={"now": {"max_calls": 2}})
        results = [(await gw.call("now", {}))[1] for _ in range(3)]
        self.assertEqual(results, [False, False, True])

    async def test_bad_arguments_become_a_tool_error_not_a_crash(self):
        gw = self.gateway()
        out, err = await gw.call("read_file", {"wrong": 1})
        self.assertTrue(err)
        out, err = await gw.call("read_file", None)  # missing arguments entirely
        self.assertTrue(err)

    async def test_every_call_is_audited_including_refusals(self):
        gw = self.gateway()
        await gw.call("now", {})
        await gw.call("write_file", {"path": "a", "content": "b"})
        self.assertEqual([e["status"] for e in self.log(gw)], ["ok", "denied"])


def fake_mcp(script=None):
    """A stand-in for the low-level server API, recording what the glue registers."""
    seen = {"script": script}

    class Tool:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class TextContent(Tool):
        pass

    class Server:
        def __init__(self, name, instructions=None):
            seen["name"], seen["instructions"] = name, instructions

        def list_tools(self):
            def deco(fn):
                seen["list_tools"] = fn
                return fn
            return deco

        def call_tool(self):
            def deco(fn):
                seen["call_tool"] = fn
                return fn
            return deco

        def create_initialization_options(self):
            return {}

        async def run(self, read, write, options):
            seen["ran"] = True
            if seen.get("script"):  # play the client while the server is "running", as the SDK does
                await seen["script"](seen)

    @contextlib.asynccontextmanager
    async def stdio_server():
        yield (None, None)

    mods = {n: types.ModuleType(n) for n in ("mcp", "mcp.server", "mcp.server.lowlevel",
                                             "mcp.server.stdio", "mcp.types")}
    mods["mcp.server.lowlevel"].Server = Server
    mods["mcp.server.stdio"].stdio_server = stdio_server
    mods["mcp.types"].Tool, mods["mcp.types"].TextContent = Tool, TextContent
    return mods, seen


class ServeGlueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old = aios.WORKSPACE
        self.addCleanup(setattr, aios, "WORKSPACE", self._old)

    async def serve(self, home, script=None, **kw):
        mods, seen = fake_mcp(script)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, mods), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            await aios.serve(home=home, **kw)
        return seen, out.getvalue(), err.getvalue()

    async def test_registers_handlers_and_keeps_stdout_clean(self):
        with tempfile.TemporaryDirectory() as d:
            seen, stdout, stderr = await self.serve(d)
            self.assertEqual(stdout, "")  # stdout is the protocol channel
            self.assertIn("MCP server 'mcp'", stderr)
            self.assertTrue(seen["ran"])
            self.assertEqual(seen["name"], "aios")
            self.assertIn("untrusted", seen["instructions"])
            self.assertTrue((Path(d) / "workspace").is_dir())
            self.assertTrue((Path(d) / "aios.db").exists())

    async def test_list_and_call_handlers_use_the_gateway(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "workspace").mkdir()
            (Path(d) / "workspace" / "n.txt").write_text("data", encoding="utf-8")
            got = {}

            async def client(seen):
                got["tools"] = await seen["list_tools"]()
                got["read"] = await seen["call_tool"]("read_file", {"path": "n.txt"})
                try:
                    await seen["call_tool"]("write_file", {"path": "x", "content": "y"})
                except RuntimeError as e:  # errors surface to the client as tool errors
                    got["error"] = str(e)

            await self.serve(d, client)
            self.assertEqual({t.name for t in got["tools"]}, set(aios.SERVE_DEFAULT_TOOLS))
            self.assertTrue(all(isinstance(t.inputSchema, dict) for t in got["tools"]))
            self.assertEqual(got["read"][0].type, "text")
            self.assertIn("data", got["read"][0].text)
            self.assertIn("<untrusted", got["read"][0].text)
            self.assertIn("permission denied", got["error"])

    async def test_policy_selects_agent_permissions(self):
        pol = {"gw": {"permissions": {"now", "write_file"},
                      "scopes": {"write_file": {"paths": ["o/*"], "auto_paths": ["o/*"]}}}}
        got = {}

        async def client(seen):
            got["names"] = {t.name for t in await seen["list_tools"]()}
            await seen["call_tool"]("write_file", {"path": "o/a.txt", "content": "hi"})

        with tempfile.TemporaryDirectory() as d:
            await self.serve(d, client, agent_name="gw", policy=pol)
            self.assertEqual(got["names"], {"now", "write_file"})
            self.assertEqual((Path(d) / "workspace" / "o" / "a.txt").read_text(encoding="utf-8"), "hi")

    async def test_audit_lands_in_the_home_database(self):
        async def client(seen):
            await seen["call_tool"]("now", {})

        with tempfile.TemporaryDirectory() as d:
            await self.serve(d, client)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                aios.audit_cmd(None, 10, d)
            self.assertIn("now", out.getvalue())
            self.assertIn("ok", out.getvalue())

    async def test_the_database_is_closed_when_the_server_stops(self):
        with tempfile.TemporaryDirectory() as d:
            await self.serve(d)
            Path(d, "aios.db").unlink()  # fails on Windows if the handle were still open


class AuditCommandTests(unittest.TestCase):
    def test_missing_database_is_reported_without_creating_it(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                aios.audit_cmd(None, 10, d)
            self.assertIn("no audit log", out.getvalue())
            self.assertFalse((Path(d) / "aios.db").exists())

    def test_filter_by_agent(self):
        with tempfile.TemporaryDirectory() as d:
            st = aios.Store(str(Path(d) / "aios.db"))
            st.log_audit("a", "now", {}, "x", "ok")
            st.log_audit("b", "read_file", {}, "x", "denied")
            st.db.close()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                aios.audit_cmd("b", 10, d)
            self.assertIn("read_file", out.getvalue())
            self.assertNotIn(" now ", out.getvalue())


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "aios.py"), *args], capture_output=True,
                              text=True, cwd=tempfile.gettempdir(), timeout=60)

    def test_serve_help(self):
        r = self.run_cli("serve", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--policy", r.stdout)
        self.assertIn("--home", r.stdout)

    def test_serve_without_the_sdk_explains_how_to_install_it(self):
        try:
            import mcp  # noqa: F401
            self.skipTest("mcp is installed here")
        except ImportError:
            pass
        r = self.run_cli("serve")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("pip install mcp", r.stderr)

    def test_bad_policy_fails_before_serving(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d, "p.toml")
            bad.write_text('[agents.x.scopes.write_file]\npath = ["a"]\n', encoding="utf-8")
            r = self.run_cli("serve", "--policy", str(bad))
            self.assertEqual(r.returncode, 2)
            self.assertIn("--policy", r.stderr)


if __name__ == "__main__":
    unittest.main()
