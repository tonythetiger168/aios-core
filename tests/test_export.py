"""aios export: an agent's conversation as OpenAI-format chat JSON."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aios  # noqa: E402
import aios_export  # noqa: E402
from test_aios import FakeProvider, Resp, text, tool_use  # noqa: E402


class ExportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self._old = aios.WORKSPACE
        aios.WORKSPACE = self.home / "workspace"
        aios.WORKSPACE.mkdir()
        self.addCleanup(setattr, aios, "WORKSPACE", self._old)
        (aios.WORKSPACE / "doc.txt").write_text("hello", encoding="utf-8")

    async def run_once(self, name="main", ephemeral=False):
        k = aios.Kernel(store=aios.Store(str(self.home / "aios.db")), provider=FakeProvider(
            Resp([tool_use("read_file", {"path": "doc.txt"}, "r1")], "tool_use"),
            Resp([tool_use("write_file", {"path": "x.txt", "content": "no"}, "w1")], "tool_use"),
            Resp([text("it says hello")], "end_turn")))
        aios.install_builtin_tools(k)  # no approver: write_file is refused
        await k._run(aios.Agent(name, {"read_file", "write_file"}, ephemeral=ephemeral), "what is in doc.txt?")
        k.store.db.close()

    def export(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = aios_export.run(str(self.home), *args)
        return code, json.loads(buf.getvalue()) if code == 0 else buf.getvalue()

    async def test_saved_history(self):
        await self.run_once()
        code, msgs = self.export("main", False, 10, None)
        self.assertEqual(code, 0)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "tool", "assistant", "tool", "assistant"])
        self.assertEqual(msgs[1]["tool_calls"][0]["function"]["name"], "read_file")
        self.assertIn('<untrusted source="read_file">', msgs[2]["content"])
        self.assertEqual(msgs[-1]["content"], "it says hello")

    async def test_rebuilt_from_the_audit_log_for_runs_without_history(self):
        await self.run_once("job-x", ephemeral=True)
        await self.run_once("job-x", ephemeral=True)
        self.assertEqual(self.export("job-x", False, 10, None)[0], 1)  # nothing saved
        code, msgs = self.export("job-x", True, 1, None)
        self.assertEqual(code, 0)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "tool", "assistant", "tool", "assistant"])
        self.assertEqual(msgs[0]["content"], "what is in doc.txt?")
        self.assertEqual(json.loads(msgs[3]["tool_calls"][0]["function"]["arguments"])["path"], "x.txt")
        self.assertEqual(msgs[4]["status"], "denied")
        self.assertIn("stop=finished", msgs[-1]["content"])
        self.assertEqual(len(self.export("job-x", True, 5, None)[1]), 12)

    async def test_to_a_file_and_images_are_left_out(self):
        st = aios.Store(str(self.home / "aios.db"))
        st.add_message("m", {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]})
        st.add_message("m", {"role": "assistant", "content": [{"type": "text", "text": "a cat"}]})
        st.db.close()
        out = self.home / "m.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(aios_export.run(str(self.home), "m", False, 10, str(out)), 0)
        msgs = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(msgs[0]["content"][1], {"type": "text", "text": "[image omitted]"})


if __name__ == "__main__":
    unittest.main()
