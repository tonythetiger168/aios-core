"""aios doctor: a report of what this machine gives AIOS, and its exit code."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.addCleanup(setattr, aios, "SANDBOX_CHOICE", aios.SANDBOX_CHOICE)

    def doctor(self, **env):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            code = aios.doctor_cmd(str(self.home))
        return code, out.getvalue()

    def test_reports_each_sandbox_and_never_prints_a_key(self):
        code, out = self.doctor(ANTHROPIC_API_KEY="sk-very-secret")
        self.assertEqual(code, 0)
        for part in ("sandboxes (run_python):", "docker", "microvm", "picks", "ANTHROPIC_API_KEY", "set",
                     "aios.db            not created yet"):
            self.assertIn(part, out)
        self.assertNotIn("sk-very-secret", out)

    def test_a_tampered_audit_log_is_a_problem(self):
        st = aios.Store(str(self.home / "aios.db"))
        st.log_audit("a", "x", {}, "r", "ok")
        st.log_audit("a", "y", {}, "r", "ok")
        code, out = self.doctor()
        self.assertEqual(code, 0)
        self.assertIn("hash chain intact (2 entries)", out)
        st.db.execute("UPDATE audit SET result='edited' WHERE tool='x'")
        st.db.commit()
        st.db.close()
        code, out = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("!! audit log          BROKEN", out)

    def test_an_asked_for_sandbox_that_cannot_run_is_a_problem(self):
        aios.SANDBOX_CHOICE = "docker"
        with mock.patch.object(aios.DockerSandbox, "unavailable", lambda *a, **k: "docker is not installed"):
            code, out = self.doctor()
        self.assertEqual(code, 1)
        self.assertIn("!! docker picks", out)
        self.assertIn("--sandbox docker: docker is not installed", out)

    def test_the_cli_runs_it(self):
        with mock.patch.object(sys, "argv", ["aios", "doctor", "--home", str(self.home)]), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                aios.cli()
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("AIOS on ", out.getvalue())


if __name__ == "__main__":
    unittest.main()
