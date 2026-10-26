"""Workspace checkpoints: write_file saves what was there, `aios undo` puts it back."""
import asyncio
import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_checkpoint as cp  # noqa: E402


class Scripted:
    def __init__(self, *script):
        self.script = list(script)

    async def complete(self, system, messages, tools, max_tokens=1024):
        return self.script.pop(0)


async def yes(*args, **kw):
    return True


def write(path, content, id):
    return aios.Completion([{"type": "tool_use", "id": id, "name": "write_file",
                             "input": {"path": path, "content": content}}], "tool_use")


class CheckpointCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name).resolve()
        self.ws = self.home / "workspace"
        self.ws.mkdir()
        self._old = aios.WORKSPACE
        aios.WORKSPACE = self.ws
        self.addCleanup(setattr, aios, "WORKSPACE", self._old)

    def run_writes(self, *writes, agent="main"):
        """One kernel run that calls write_file for each (path, content); returns the run id."""
        steps = [write(p, c, f"w{i}") for i, (p, c) in enumerate(writes)]
        done = aios.Completion([{"type": "text", "text": "ok"}], "end_turn")
        k = aios.Kernel(store=aios.Store(str(self.home / "aios.db")), provider=Scripted(*steps, done), approver=yes)
        aios.install_builtin_tools(k, sandbox="off")
        a = aios.Agent(agent, {"write_file"})
        asyncio.run(k._run(a, "go", aios.Budget(max_steps=len(writes) + 1)))
        k.store.db.close()
        return a.run_id

    def undo(self, *flags):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["aios", "undo", "--home", str(self.home), *flags]), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as e:
            aios.cli()
        return e.exception.code, out.getvalue()

    def read(self, rel):
        return (self.ws / rel).read_text(encoding="utf-8")


class UndoTests(CheckpointCase):
    def test_undo_restores_overwritten_and_removes_created_files(self):
        (self.ws / "notes.txt").write_text("mine", encoding="utf-8")
        self.run_writes(("notes.txt", "agent v1"), ("notes.txt", "agent v2"), ("new/plan.md", "plan"))
        self.assertEqual(self.read("notes.txt"), "agent v2")
        code, out = self.undo()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read("notes.txt"), "mine")  # back to before the run, not to v1
        self.assertFalse((self.ws / "new" / "plan.md").exists())
        self.assertIn("restored", out)
        self.assertEqual(self.undo()[1].strip(), "(nothing to undo)")
        st = aios.Store(str(self.home / "aios.db"))
        self.addCleanup(st.db.close)
        self.assertEqual(st.audit_tail(n=1)[0]["tool"], "undo")

    def test_newer_changes_are_kept_unless_forced(self):
        first = self.run_writes(("a.txt", "run 1"))
        self.run_writes(("a.txt", "run 2"))
        code, out = self.undo("--run", first)
        self.assertEqual(code, 1)
        self.assertIn("changed after this run", out)
        self.assertEqual(self.read("a.txt"), "run 2")
        self.assertEqual(self.undo()[0], 0)  # the latest run first...
        self.assertEqual(self.read("a.txt"), "run 1")
        self.assertEqual(self.undo("--run", first)[0], 0)  # ...then the earlier one applies
        self.assertFalse((self.ws / "a.txt").exists())

    def test_hand_edit_after_the_run_is_protected(self):
        self.run_writes(("a.txt", "agent"))
        (self.ws / "a.txt").write_text("my edit", encoding="utf-8")
        self.assertEqual(self.undo()[0], 1)
        self.assertEqual(self.read("a.txt"), "my edit")
        self.assertEqual(self.undo("--force")[0], 0)
        self.assertFalse((self.ws / "a.txt").exists())

    def test_dry_run_and_list(self):
        self.run_writes(("a.txt", "x"), agent="writer")
        code, out = self.undo("--dry-run")
        self.assertIn("(dry run) a.txt", out)
        self.assertTrue((self.ws / "a.txt").exists())
        code, out = self.undo("--list", "--agent", "writer")
        self.assertIn("writer", out)
        self.assertIn("a.txt", out)
        self.assertNotIn("undone", out)

    def test_agent_filter_picks_that_agents_latest_run(self):
        self.run_writes(("a.txt", "a"), agent="alpha")
        self.run_writes(("b.txt", "b"), agent="beta")
        self.assertEqual(self.undo("--agent", "alpha")[0], 0)
        self.assertFalse((self.ws / "a.txt").exists())
        self.assertTrue((self.ws / "b.txt").exists())


class CheckpointModuleTests(CheckpointCase):
    def setUp(self):
        super().setUp()
        self.st = aios.Store(":memory:")
        self.addCleanup(self.st.db.close)

    def test_large_previous_file_is_not_kept(self):
        p = self.ws / "big.bin"
        p.write_bytes(b"x" * (cp.MAX_BYTES + 1))
        cp.record(self.st.db, "main", "r1", self.ws, p, b"small")
        p.write_bytes(b"small")
        (row,) = cp.undo(self.st.db, self.ws, "r1")
        self.assertIn("not kept", row[1])
        self.assertEqual(p.read_bytes(), b"small")

    def test_old_versions_are_pruned(self):
        p = self.ws / "a.txt"
        cp.record(self.st.db, "main", "old", self.ws, p, b"1", now=datetime.now() - timedelta(days=cp.KEEP_DAYS + 1))
        cp.record(self.st.db, "main", "new", self.ws, p, b"2")
        self.assertEqual([r["run"] for r in cp.runs(self.st.db)], ["new"])

    def test_paths_outside_the_workspace_are_refused(self):
        cp.ensure(self.st.db)
        self.st.db.execute("INSERT INTO file_versions(ts, agent, run, path, existed, after_sha) "
                           "VALUES('2026-01-01T00:00:00', 'm', 'r', '../escape.txt', 0, '')")
        (row,) = cp.undo(self.st.db, self.ws, "r", force=True)
        self.assertIn("outside the workspace", row[1])


if __name__ == "__main__":
    unittest.main()
