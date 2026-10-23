"""Watchdog rules over the audit log and job runs, the daemon heartbeat, and the status/alerts commands."""
import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_watchdog as wd  # noqa: E402

TAINTED = ("not approved by a human: write_file (this run has read untrusted content, so a person must "
           "approve each risky call interactively; auto-approve does not apply)")


class WatchCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.st = aios.Store(str(self.home / "aios.db"))
        self.addCleanup(self.st.db.close)

    def rules(self, now=None):
        return sorted((a["rule"], a["subject"]) for a in wd.check(self.st.db, now))


class RuleTests(WatchCase):
    def test_a_quiet_log_raises_nothing(self):
        self.st.log_audit("main", "read_file", {"path": "a"}, "text", "ok")
        self.st.log_audit("main", "write_file", {"path": "a"}, "permission denied: write_file", "denied")
        self.assertEqual(self.rules(), [])

    def test_a_refused_call_after_untrusted_content_is_critical(self):
        self.st.log_audit("job-mail", "write_file", {"path": "x"}, TAINTED, "denied")
        added = wd.check(self.st.db)
        self.assertEqual([(a["rule"], a["severity"], a["subject"]) for a in added],
                         [("tainted-refused", "critical", "job-mail")])
        self.assertIn("untrusted content", added[0]["detail"])

    def test_an_unattended_job_trying_a_risky_call(self):
        self.st.log_audit("job-digest", "run_python", {"code": "1"}, "not approved by a human: run_python", "denied")
        self.st.log_audit("main", "run_python", {"code": "1"}, "not approved by a human: run_python", "denied")
        self.assertEqual(self.rules(), [("unattended-refused", "job-digest")])  # chat refusals are normal

    def test_refusal_spike(self):
        for _ in range(wd.DENIED_SPIKE):
            self.st.log_audit("a2a-guest", "read_file", {"path": "../x"}, "permission denied: path", "denied")
        self.assertEqual(self.rules(), [("refusal-spike", "a2a-guest")])

    def test_budget_stop(self):
        self.st.log_audit("main", wd.RUN_ENTRY, {"task": "t"}, "steps=3 stop=budget", "budget")
        self.st.log_audit("main", wd.RUN_ENTRY, {"task": "t"}, "steps=1 stop=finished", "ok")
        self.assertEqual(self.rules(), [("budget-stop", "main")])

    def test_job_failing_only_after_consecutive_failures(self):
        for status in ("error", "timeout"):
            self.st.add_run("sync", "2026-10-23T09:00:00", status, "boom")
        self.assertEqual(self.rules(), [])
        self.st.add_run("sync", "2026-10-23T09:05:00", "skipped", "daily cap")  # skipped does not count
        self.st.add_run("sync", "2026-10-23T09:10:00", "error", "boom")
        self.assertEqual(self.rules(), [("job-failing", "job-sync")])
        self.st.add_run("ok-job", "2026-10-23T09:00:00", "error", "boom")
        self.st.add_run("ok-job", "2026-10-23T09:01:00", "error", "boom")
        self.st.add_run("ok-job", "2026-10-23T09:02:00", "ok", "fine")
        self.assertEqual(self.rules(), [])  # recovered; sync is still in its cooldown

    def test_old_entries_are_outside_the_window(self):
        self.st.log_audit("job-mail", "write_file", {"path": "x"}, TAINTED, "denied")
        later = datetime.now() + timedelta(minutes=wd.WINDOW_MINUTES + 1)
        self.assertEqual(self.rules(later), [])

    def test_cooldown_then_raised_again(self):
        self.st.log_audit("job-mail", "write_file", {"path": "x"}, TAINTED, "denied")
        self.assertEqual(len(wd.check(self.st.db)), 1)
        self.assertEqual(wd.check(self.st.db), [])  # same rule + subject inside the cooldown
        self.assertEqual(len(wd.list_alerts(self.st.db)), 1)
        later = datetime.now() + timedelta(minutes=wd.COOLDOWN_MINUTES + 1)
        self.st.db.execute("UPDATE audit SET ts=?", (later.isoformat(timespec="seconds"),))
        self.assertEqual(len(wd.check(self.st.db, later)), 1)

    def test_ack_one_and_all(self):
        self.st.log_audit("job-a", "write_file", {}, TAINTED, "denied")
        self.st.log_audit("job-b", "write_file", {}, TAINTED, "denied")
        first, second = wd.check(self.st.db)
        self.assertEqual(wd.ack(self.st.db, str(first["id"])), 1)
        self.assertEqual(wd.ack(self.st.db, str(first["id"])), 0)  # already acknowledged
        self.assertEqual([a["id"] for a in wd.list_alerts(self.st.db)], [second["id"]])
        self.assertEqual(wd.ack(self.st.db, "all"), 1)
        self.assertEqual(wd.list_alerts(self.st.db), [])
        self.assertEqual(len(wd.list_alerts(self.st.db, include_acked=True)), 2)

    def test_the_watchdog_never_changes_the_audit_log(self):
        self.st.log_audit("job-mail", "write_file", {"path": "x"}, TAINTED, "denied")
        before = self.st.db.execute("SELECT * FROM audit").fetchall()
        wd.check(self.st.db)
        self.assertEqual(self.st.db.execute("SELECT * FROM audit").fetchall(), before)


class HeartbeatTests(WatchCase):
    def test_never_alive_down(self):
        self.assertEqual(wd.liveness(self.st.db)["state"], "never")
        t = datetime(2026, 10, 23, 9, 0, 0)
        wd.beat(self.st.db, "daemon", "pid=1 tick=10 timeout=300", now=t)
        self.assertEqual(wd.liveness(self.st.db, now=t + timedelta(seconds=30))["state"], "alive")
        self.assertEqual(wd.liveness(self.st.db, now=t + timedelta(seconds=wd.STALE_AFTER + 1))["state"], "down")


class CommandTests(WatchCase):
    def run_cmd(self, fn, *args, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fn(*args, **kw)
        return code, out.getvalue()

    def test_status_reports_and_exit_code(self):
        code, out = self.run_cmd(aios.status_cmd, str(self.home))
        self.assertEqual(code, 0)
        self.assertIn("daemon: never", out)
        wd.beat(self.st.db, "daemon", "pid=1 tick=10 timeout=300", now=datetime.now() - timedelta(seconds=200))
        code, out = self.run_cmd(aios.status_cmd, str(self.home))
        self.assertIn("daemon: alive", out)  # 200s < 3 ticks + one job timeout: a long job, not a dead daemon
        wd.beat(self.st.db, "daemon", "pid=1 tick=10 timeout=300", now=datetime.now() - timedelta(seconds=400))
        code, out = self.run_cmd(aios.status_cmd, str(self.home))
        self.assertEqual(code, 1)
        self.assertIn("daemon: down", out)
        wd.beat(self.st.db)
        self.st.log_audit("job-mail", "write_file", {}, TAINTED, "denied")
        wd.check(self.st.db)
        code, out = self.run_cmd(aios.status_cmd, str(self.home))
        self.assertEqual(code, 1)
        self.assertIn("open alerts: 1", out)
        self.assertIn("tainted-refused", out)

    def test_status_without_a_database(self):
        code, out = self.run_cmd(aios.status_cmd, str(self.home / "nowhere"))
        self.assertEqual(code, 1)
        self.assertFalse((self.home / "nowhere" / "aios.db").exists())  # nothing was created

    def test_alerts_check_list_and_ack(self):
        self.st.log_audit("job-mail", "write_file", {}, TAINTED, "denied")
        code, out = self.run_cmd(aios.alerts_cmd, str(self.home), run_check=True)
        self.assertIn("checked: 1 new alert", out)
        self.assertIn("tainted-refused job-mail", out)
        self.assertEqual(self.run_cmd(aios.alerts_cmd, str(self.home), ack="x1")[0], 2)
        code, out = self.run_cmd(aios.alerts_cmd, str(self.home), ack="all")
        self.assertIn("acknowledged 1", out)
        self.assertIn("(no open alerts)", self.run_cmd(aios.alerts_cmd, str(self.home))[1])
        self.assertIn("(acknowledged ", self.run_cmd(aios.alerts_cmd, str(self.home), show_all=True)[1])


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_daemon_beats_and_raises_alerts(self):
        class Boom:
            async def complete(self, *a, **kw):
                raise RuntimeError("model down")

        with tempfile.TemporaryDirectory() as d:
            st = aios.Store(str(Path(d, "aios.db")))
            for i in range(wd.JOB_FAIL_RUNS - 1):
                st.add_run("sync", f"2026-10-23T09:0{i}:00", "error", "boom")
            st.add_job("sync", "main", "sync it", "every 1h", (datetime.now() - timedelta(minutes=1))
                       .isoformat(timespec="seconds"))
            st.db.close()
            err = io.StringIO()
            old = aios.WORKSPACE
            self.addCleanup(setattr, aios, "WORKSPACE", old)
            with contextlib.redirect_stderr(err):
                await aios.daemon(d, provider=Boom(), once=True)
            self.assertIn("ALERT warning job-failing", err.getvalue())
            st = aios.Store(str(Path(d, "aios.db")))
            self.assertEqual(wd.liveness(st.db)["state"], "alive")
            self.assertIn("timeout=300", wd.liveness(st.db)["info"])
            st.db.close()


if __name__ == "__main__":
    unittest.main()
