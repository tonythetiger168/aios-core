"""MCP with the official `mcp` SDK (1.x or 2.x), when it is installed: `aios serve` driven by the SDK's
own client over stdio, and aios as a client of a real SDK server (demo_mcp_server.py).

CI installs the latest SDK on the Python 3.12 jobs and the last 1.x release on Windows 3.11, so both
API generations are checked; Ubuntu 3.11 runs without it and checks the "pip install mcp" hint."""
import asyncio
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # test_schedule helpers

import aios  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False

field = aios._sdk_field


def texts(res) -> str:
    return "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")


@unittest.skipUnless(HAVE_MCP, "needs the mcp SDK (pip install mcp)")
class ServeWithTheSdkClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        (self.home / "workspace").mkdir()
        (self.home / "workspace" / "notes.txt").write_text("meeting at noon", encoding="utf-8")

    async def session(self, check, *extra):
        params = StdioServerParameters(command=sys.executable,
                                       args=[str(ROOT / "aios.py"), "serve", "--home", str(self.home), *extra],
                                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                init = await s.initialize()
                await check(s, init)

    async def test_a_real_client_lists_and_calls_the_governed_tools(self):
        async def check(s, init):
            self.assertEqual(field(init, "server_info", "serverInfo").name, "aios")
            self.assertIn("untrusted", init.instructions or "")
            names = {t.name for t in (await s.list_tools()).tools}
            self.assertEqual(names, set(aios.SERVE_DEFAULT_TOOLS))
            schema = next(field(t, "input_schema", "inputSchema") for t in (await s.list_tools()).tools
                          if t.name == "read_file")
            self.assertIn("path", schema["properties"])
            res = await s.call_tool("read_file", {"path": "notes.txt"})
            self.assertFalse(field(res, "is_error", "isError"))
            self.assertIn("meeting at noon", texts(res))
            self.assertIn('<untrusted source="read_file">', texts(res))
            res = await s.call_tool("write_file", {"path": "x.txt", "content": "no"})
            self.assertTrue(field(res, "is_error", "isError"))
            self.assertIn("permission denied", texts(res))
            res = await s.call_tool("read_file", {"path": "../outside.txt"})
            self.assertTrue(field(res, "is_error", "isError"))

        await asyncio.wait_for(self.session(check), 60)
        self.assertFalse((self.home / "workspace" / "x.txt").exists())
        store = aios.Store(str(self.home / "aios.db"))
        self.addCleanup(store.db.close)
        log = [(e["tool"], e["status"]) for e in store.audit_tail("mcp", 10)]
        self.assertIn(("read_file", "ok"), log)
        self.assertIn(("write_file", "denied"), log)

    async def test_a_policy_section_chooses_the_offer(self):
        policy = self.home / "policy.toml"
        policy.write_text('[agents.gateway]\npermissions = ["now"]\n', encoding="utf-8")

        async def check(s, init):
            self.assertEqual([t.name for t in (await s.list_tools()).tools], ["now"])
            res = await s.call_tool("read_file", {"path": "notes.txt"})
            self.assertTrue(field(res, "is_error", "isError"))

        await asyncio.wait_for(self.session(check, "--agent", "gateway", "--policy", str(policy)), 60)


@unittest.skipUnless(HAVE_MCP, "needs the mcp SDK (pip install mcp)")
class ClientOfARealServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_of_an_sdk_server_are_registered_and_callable(self):
        k = aios.Kernel(store=aios.Store(":memory:"))
        try:
            # not wait_for: on 3.11 it runs the call in another task, and the SDK's anyio scopes must be
            # left in the task that entered them (k.close() below)
            names = await k.connect_mcp("demo", sys.executable, [str(ROOT / "demo_mcp_server.py")])
            self.assertEqual(sorted(names), ["demo__add", "demo__word_count"])
            self.assertTrue(k.tools.is_untrusted("demo__add"))
            self.assertIn("demo__add", k.needs_approval)
            agent = aios.Agent("t", {"demo__*"})
            self.assertEqual(str(await k.tools.call("demo__add", agent, a=2, b=40)), "42")
            self.assertEqual(str(await k.tools.call("demo__word_count", agent, text="one two three")), "3")
            with self.assertRaises(RuntimeError):  # a tool error from the server
                await k.tools.call("demo__add", agent, a="not a number", b=1)
        finally:
            await k.close()
            k.store.db.close()

    @unittest.skipUnless(aios.landlock_abi() >= 1 or aios.seatbelt_ok(), "needs Landlock or sandbox-exec")
    async def test_a_confined_server_still_works_and_its_state_folder_goes_away(self):
        k = aios.Kernel(store=aios.Store(":memory:"))
        made = []
        real = aios.tempfile.mkdtemp

        def spy(*a, **kw):
            made.append(real(*a, **kw))
            return made[-1]

        try:
            with mock.patch.object(aios.tempfile, "mkdtemp", spy):
                await k.connect_mcp("demo", sys.executable, [str(ROOT / "demo_mcp_server.py")], confine=True)
            agent = aios.Agent("t", {"demo__*"})
            self.assertEqual(str(await k.tools.call("demo__add", agent, a=2, b=40)), "42")
            self.assertTrue(Path(made[0]).is_dir())
        finally:
            await k.close()
            k.store.db.close()
        self.assertFalse(Path(made[0]).exists())


@unittest.skipUnless(HAVE_MCP, "needs the mcp SDK (pip install mcp)")
class DaemonWithAServerTests(unittest.IsolatedAsyncioTestCase):
    # native paths, as a user types them: backslashes on Windows
    DEMO = f'demo="{sys.executable}" "{ROOT / "demo_mcp_server.py"}"'

    def setUp(self):
        self.addCleanup(setattr, aios, "WORKSPACE", aios.WORKSPACE)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    async def run_daemon(self, specs, confine=frozenset(), offline=frozenset(), egress=None):
        from test_schedule import Scripted, iso, text
        from datetime import datetime, timedelta
        st = aios.Store(str(self.home / "aios.db"))
        st.add_job("j", "main", "add things", "every 1h", iso(datetime.now() - timedelta(minutes=1)))
        st.db.close()
        policy = self.home / "policy.toml"
        policy.write_text('[agents.main]\npermissions = ["demo__*"]\n', encoding="utf-8")
        provider, err = Scripted(text("done")), io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = await aios.daemon(str(self.home), aios.load_policy(str(policy)), provider=provider, once=True,
                                   mcp_specs=specs, confine_mcp=confine, offline_mcp=offline,
                                   egress_mcp=egress)
        return rc, err.getvalue(), provider

    async def test_jobs_get_the_tools_the_policy_grants_and_an_unconfined_server_is_flagged(self):
        rc, err, provider = await self.run_daemon([self.DEMO])
        self.assertNotEqual(rc, 1)
        self.assertIn("demo__add", provider.calls[0]["tools"])
        self.assertIn("job j: ok", err)
        self.assertIn("WARNING: MCP server 'demo'", err)

    @unittest.skipUnless(aios.landlock_abi() >= 1 or aios.seatbelt_ok(), "needs Landlock or sandbox-exec")
    async def test_a_confined_server_is_not_flagged(self):
        rc, err, provider = await self.run_daemon([self.DEMO],
                                                  {"demo"})
        self.assertIn("demo__add", provider.calls[0]["tools"])
        self.assertIn("MCP demo (confined)", err)
        self.assertNotIn("WARNING: MCP server", err)

    @unittest.skipUnless(aios.offline_level(), "needs a network namespace, Landlock ABI 4 or sandbox-exec")
    async def test_an_offline_server_works_and_is_not_flagged(self):
        rc, err, provider = await self.run_daemon([self.DEMO], offline={"demo"})
        self.assertIn("demo__add", provider.calls[0]["tools"])
        self.assertIn("job j: ok", err)
        self.assertIn("MCP demo (offline", err)
        self.assertNotIn("WARNING: MCP server", err)

    @unittest.skipUnless(aios.egress_ok(), "needs a network namespace or sandbox-exec")
    async def test_a_server_with_an_egress_allowlist_works_and_says_where_it_may_go(self):
        rc, err, provider = await self.run_daemon([self.DEMO], {"demo"},
                                                  egress={"demo": [("api.github.com", 443), ("*.example.com", 8443)]})
        self.assertIn("demo__add", provider.calls[0]["tools"])
        self.assertIn("job j: ok", err)
        self.assertIn("MCP demo (network: only api.github.com, *.example.com:8443)", err)
        self.assertNotIn("WARNING: MCP server", err)

    async def test_a_server_that_does_not_start_stops_the_daemon_before_any_job(self):
        rc, err, provider = await self.run_daemon(["demo=no-such-program-aios"])
        self.assertEqual(rc, 1)
        self.assertEqual(provider.calls, [])
        self.assertIn("--mcp:", err)
        (self.home / "aios.db").unlink()  # fails on Windows if the daemon left the database open


class McpFlagTests(unittest.TestCase):
    def parse(self, *argv):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["aios", "daemon", *argv]), contextlib.redirect_stderr(err), \
                mock.patch.object(aios, "daemon") as d, self.assertRaises(SystemExit):
            aios.cli()
        d.assert_not_called()
        return err.getvalue()

    def test_windows_paths_keep_their_backslashes(self):
        self.assertEqual(aios.split_command(r'C:\Py\python.exe "C:\My Tools\s.py" --x', windows=True),
                         [r"C:\Py\python.exe", r"C:\My Tools\s.py", "--x"])
        self.assertEqual(aios.split_command(r'python a\ b.py "#1"', windows=False), ["python", "a b.py", "#1"])

    def test_bad_specs_and_unknown_confine_names_are_refused(self):
        self.assertIn("NAME=COMMAND", self.parse("--mcp", "python server.py"))
        self.assertIn("names no --mcp server: other", self.parse("--mcp", "demo=python s.py", "--confine-mcp", "other"))
        self.assertIn("--offline-mcp names no --mcp server: x", self.parse("--mcp", "demo=python s.py",
                                                                           "--offline-mcp", "x"))
        with mock.patch.object(aios, "offline_level", return_value=""):
            self.assertIn("cannot cut a server off", self.parse("--mcp", "demo=python s.py", "--offline-mcp", "demo"))

    def test_offline_servers_are_confined_too(self):
        with mock.patch.object(sys, "argv", ["aios", "daemon", "--mcp", "demo=python s.py", "--offline-mcp", "demo"]), \
                mock.patch.object(aios, "offline_level", return_value="all"), \
                mock.patch.object(aios, "daemon", mock.Mock()) as d, mock.patch.object(aios.asyncio, "run"), \
                mock.patch.object(aios, "build_model", return_value=(None, None)):
            aios.cli()
        self.assertEqual(d.call_args.args[-3:], ({"demo"}, {"demo"}, {}))

    def test_egress_flags(self):
        with mock.patch.object(sys, "argv", ["aios", "daemon", "--mcp", "gh=python s.py", "--mcp-egress",
                                             "gh=api.github.com, *.githubusercontent.com"]), \
                mock.patch.object(aios, "egress_ok", return_value=True), \
                mock.patch.object(aios, "daemon", mock.Mock()) as d, mock.patch.object(aios.asyncio, "run"), \
                mock.patch.object(aios, "build_model", return_value=(None, None)):
            aios.cli()
        self.assertEqual(d.call_args.args[-3:], ({"gh"}, set(), {"gh": [("api.github.com", 443),
                                                                        ("*.githubusercontent.com", 443)]}))
        spec = ("--mcp", "gh=python s.py")
        with mock.patch.object(aios, "egress_ok", return_value=True):
            self.assertIn("NAME=HOST", self.parse(*spec, "--mcp-egress", "api.github.com"))
            self.assertIn("bad host 'https://x'", self.parse(*spec, "--mcp-egress", "gh=https://x"))
            self.assertIn("give each server once", self.parse(*spec, "--mcp-egress", "gh=a.com", "--mcp-egress", "gh=b.com"))
            self.assertIn("--mcp-egress names no --mcp server: x", self.parse(*spec, "--mcp-egress", "x=a.com"))
            self.assertIn("not both", self.parse(*spec, "--mcp-egress", "gh=a.com", "--offline-mcp", "gh"))
        with mock.patch.object(aios, "egress_ok", return_value=False):
            self.assertIn("cannot cut a server off the network completely",
                          self.parse(*spec, "--mcp-egress", "gh=a.com"))


if __name__ == "__main__":
    unittest.main()
