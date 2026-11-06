"""The supervisor (aios_supervisor): AIOS answers its own watchdog alerts by pausing jobs and holding
agents, never by adding risk, and only a person undoes it."""
import asyncio
import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_supervisor as sup  # noqa: E402
import aios_watchdog as wd  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TAINTED = ("not approved by a human: write_file (this run has read untrusted content, so a person must "
           "approve each risky call interactively; auto-approve does not apply)")


class Case(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = Path(d.name)
        self.st = aios.Store(str(self.home / "aios.db"))
        self.addCleanup(self.st.db.close)
        self.db = self.st.db
        sup.tick(self.db)  # the first look: sets the cursor

    def job(self, name, agent="digest"):
        self.st.add_job(name, agent, "do it", "every 1h", datetime.now().isoformat(timespec="seconds"))

    def enabled(self, name):
        return self.st.get_job(name)["enabled"]

    def cycle(self):
        wd.check(self.db)
        return sup.tick(self.db)

    def fail_job(self, name):
        for _ in range(wd.JOB_FAIL_RUNS):
            self.st.add_run(name, datetime.now().isoformat(timespec="seconds"), "error", "boom")


class ResponseTests(Case):
    def test_a_failing_job_is_paused_and_audited(self):
        self.job("mail")
        self.fail_job("mail")
        acts = self.cycle()
        self.assertEqual([(a["action"], a["target"]) for a in acts], [("pause-job", "mail")])
        self.assertFalse(self.enabled("mail"))
        tail = self.st.audit_tail("supervisor", 5)
        self.assertEqual(tail[0]["tool"], sup.TOOL)
        self.assertTrue(self.st.verify_audit()["ok"])  # its entry is chained like any other
        self.assertEqual(self.cycle(), [])  # nothing twice

    def test_a_job_trying_risky_calls_after_injection_is_paused(self):
        self.job("inbox")
        self.st.log_audit("job-inbox", "write_file", {"path": "x"}, TAINTED, "denied")
        self.assertEqual([(a["action"], a["target"]) for a in self.cycle()], [("pause-job", "inbox")])

    def test_a_job_copying_untrusted_arguments_is_paused(self):
        self.job("fetcher")
        wd.alert(self.db, "untrusted-args", "job-fetcher", "copied a URL from a fetched page")
        self.assertEqual([(a["action"], a["target"]) for a in sup.tick(self.db)], [("pause-job", "fetcher")])

    def test_a_non_job_agent_is_held_not_stopped(self):
        for _ in range(wd.DENIED_SPIKE):
            self.st.log_audit("a2a-guest", "read_file", {"path": "../x"}, "permission denied: path", "denied")
        acts = self.cycle()
        self.assertEqual([(a["action"], a["target"]) for a in acts], [("hold-agent", "a2a-guest")])
        self.assertIn("refusal-spike", sup.held(self.db, "a2a-guest"))
        self.assertIsNone(sup.held(self.db, "main"))

    def test_budget_stops_alone_only_notify(self):
        self.job("sum")
        self.st.log_audit("job-sum", wd.RUN_ENTRY, {"task": "t"}, "stop=budget", "budget")
        self.assertEqual([a["action"] for a in self.cycle()], ["notify"])
        self.assertTrue(self.enabled("sum"))

    def test_a_job_that_keeps_hitting_its_budget_is_paused(self):
        self.job("sum")
        for _ in range(sup.BUDGET_RUNAWAY):
            self.st.log_audit("job-sum", wd.RUN_ENTRY, {"task": "t"}, "stop=budget", "budget")
        acts = self.cycle()
        self.assertIn(("pause-job", "sum"), [(a["action"], a["target"]) for a in acts])
        self.assertFalse(self.enabled("sum"))

    def test_a_tampered_audit_log_is_safe_mode(self):
        self.job("a")
        self.job("b")
        self.st.log_audit("main", "read_file", {}, "x", "ok")
        self.st.log_audit("main", "read_file", {}, "y", "ok")
        self.db.execute("UPDATE audit SET result='edited' WHERE id=1")
        self.db.commit()
        acts = self.cycle()
        self.assertEqual(sorted(a["action"] for a in acts), ["pause-job", "pause-job", "safe-mode"])
        self.assertFalse(self.enabled("a") or self.enabled("b"))
        self.assertIsNotNone(sup.held(self.db, "anyone"))  # '*' holds every agent

    def test_old_alerts_on_a_new_database_are_left_to_a_person(self):
        self.db.execute("DELETE FROM supervisor_state")
        self.job("old")
        then = datetime.now() - timedelta(hours=3)
        wd.alert(self.db, "job-failing", "job-old", "long ago", now=then)
        self.assertEqual(sup.tick(self.db), [])
        self.assertTrue(self.enabled("old"))

    def test_acknowledged_alerts_are_not_acted_on(self):
        self.job("x")
        a = wd.alert(self.db, "job-failing", "job-x", "failing")
        wd.ack(self.db, str(a["id"]))
        self.assertEqual(sup.tick(self.db), [])
        self.assertTrue(self.enabled("x"))


class ReleaseTests(Case):
    def test_a_person_resumes_a_paused_job(self):
        self.job("mail")
        self.fail_job("mail")
        aid = self.cycle()[0]["id"]
        self.assertEqual(sup.release(self.db, str(aid)), [f"#{aid}: job mail resumed"])
        self.assertTrue(self.enabled("mail"))
        self.assertEqual(self.st.audit_tail("human", 1)[0]["tool"], sup.TOOL)
        self.assertEqual(sup.release(self.db, str(aid)), [])  # once only

    def test_release_all_lifts_holds(self):
        wd.alert(self.db, "loop-stop", "main", "looping")
        self.cycle()
        self.assertIsNotNone(sup.held(self.db, "main"))
        sup.release(self.db, "all")
        self.assertIsNone(sup.held(self.db, "main"))

    def test_a_job_a_person_disabled_is_not_reenabled_by_release_of_another(self):
        self.job("a")
        self.job("b")
        self.fail_job("a")
        self.cycle()
        self.st.set_job("b", enabled=False)
        sup.release(self.db, "all")
        self.assertTrue(self.enabled("a"))
        self.assertFalse(self.enabled("b"))


class HoldInKernelTests(unittest.TestCase):
    def test_a_held_agent_needs_a_person_for_risky_calls(self):
        with tempfile.TemporaryDirectory() as d:
            st = aios.Store(os.path.join(d, "aios.db"))
            try:
                k = aios.Kernel(store=st, provider=None)
                aios.install_builtin_tools(k)
                agent = aios.Agent("main", {"write_file"}, scopes={"write_file": {"auto_paths": ["*"]}})
                old = aios.WORKSPACE
                aios.WORKSPACE = Path(d).resolve()
                try:
                    out, err, status = asyncio.run(k._execute(agent, "write_file",
                                                              {"path": "a.txt", "content": "x"}, False, {}))
                    self.assertEqual(status, "ok")  # auto_paths pre-allow it
                    sup.ensure(st.db)
                    st.db.execute("INSERT INTO holds(agent, ts, reason, action_id) VALUES('main','t','[loop-stop] x',1)")
                    st.db.commit()
                    out, err, status = asyncio.run(k._execute(agent, "write_file",
                                                              {"path": "b.txt", "content": "x"}, False, {}))
                    self.assertEqual(status, "denied")
                    self.assertIn("supervisor put this agent on hold", out)
                    self.assertNotIn("untrusted content", out)  # not mistaken for an injection sign
                    self.assertFalse((Path(d) / "b.txt").exists())
                finally:
                    aios.WORKSPACE = old
            finally:
                st.db.close()


class GuardrailTests(unittest.TestCase):
    def test_the_daemon_path_never_loosens_anything(self):
        src = (ROOT / "aios_supervisor.py").read_text(encoding="utf-8")
        body = src.split("def release(")[0]  # everything the daemon can reach
        self.assertNotRegex(body, r"SET\s+enabled\s*=\s*1")
        self.assertNotRegex(body, r"DELETE\s+FROM\s+holds")
        self.assertNotRegex(body, r"(UPDATE|DELETE\s+FROM)\s+(audit|approvals)\b")
        self.assertNotIn("decide(", src)
        for a in sup.ACTIONS:
            self.assertIn(a, ("pause-job", "hold-agent", "safe-mode", "notify"))

    def test_release_is_only_called_by_the_cli(self):
        for p in ROOT.glob("aios*.py"):
            if p.name == "aios_supervisor.py":
                continue
            calls = re.findall(r"sup(?:ervisor)?\.release\(", p.read_text(encoding="utf-8"))
            self.assertLessEqual(len(calls), 1 if p.name == "aios.py" else 0, p.name)
        src = (ROOT / "aios.py").read_text(encoding="utf-8")
        self.assertIn("sup.release(", src.split("def supervisor_cmd(")[1].split("\ndef ")[0])


class CommandTests(Case):
    def out(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = aios.supervisor_cmd(str(self.home), *argv)
        return code, buf.getvalue()

    def test_list_and_release(self):
        self.job("mail")
        self.fail_job("mail")
        aid = self.cycle()[0]["id"]
        code, text = self.out()
        self.assertIn(f"#{aid}", text)
        self.assertIn("pause-job mail", text)
        self.assertIn("1 job(s) paused", text)
        code, text = self.out(str(aid))
        self.assertIn("job mail resumed", text)

    def test_status_shows_it(self):
        wd.alert(self.db, "loop-stop", "main", "looping")
        self.cycle()
        wd.ack(self.db, "all")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = aios.status_cmd(str(self.home))
        self.assertIn("on hold: main", buf.getvalue())
        self.assertEqual(code, 1)


class TelegramTests(Case):
    def test_new_actions_go_to_the_phone(self):
        import aios_telegram as tg
        from tests.test_telegram import FakeAPI
        api = FakeAPI()
        b = tg.Bridge(str(self.home), 111, api, log=lambda m: None)
        self.job("old")
        self.fail_job("old")
        self.cycle()
        b.send_actions()  # first look: earlier actions are not re-sent
        self.assertEqual(api.sent("sendMessage"), [])
        wd.alert(self.db, "loop-stop", "main", "looping")
        sup.tick(self.db)
        b.send_actions()
        msgs = api.sent("sendMessage")
        self.assertEqual(len(msgs), 1)
        self.assertIn("hold-agent main", msgs[0]["text"])
        self.assertNotIn("reply_markup", msgs[0])


class DaemonTests(unittest.TestCase):
    def test_the_daemon_pauses_a_failing_job_and_no_supervise_does_not(self):
        for supervise in (True, False):
            with self.subTest(supervise=supervise), tempfile.TemporaryDirectory() as d:
                st = aios.Store(os.path.join(d, "aios.db"))
                st.add_job("mail", "x", "t", "every 1h", "2999-01-01T00:00:00")
                for _ in range(wd.JOB_FAIL_RUNS):
                    st.add_run("mail", datetime.now().isoformat(timespec="seconds"), "error", "boom")
                st.db.close()
                old = aios.WORKSPACE
                try:
                    with contextlib.redirect_stderr(io.StringIO()):
                        asyncio.run(aios.daemon(d, None, provider=None, once=True, supervise=supervise))
                finally:
                    aios.WORKSPACE = old
                st = aios.Store(os.path.join(d, "aios.db"))
                try:
                    self.assertEqual(st.get_job("mail")["enabled"], not supervise)
                finally:
                    st.db.close()


if __name__ == "__main__":
    unittest.main()
