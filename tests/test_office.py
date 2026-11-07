"""3D office: where each agent stands follows from the audit log, approvals, holds and jobs, and the page
keeps attacker-written text out of HTML."""
import json
import re
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import aios  # noqa: E402
import aios_office as office  # noqa: E402
from test_console import XSS, ConsoleCase  # noqa: E402


def ago(s: float) -> str:
    return (datetime.now() - timedelta(seconds=s)).isoformat(timespec="seconds")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
            CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, agent TEXT, tool TEXT, args TEXT,
                               result TEXT, status TEXT, hash TEXT);
            CREATE TABLE approvals(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, agent TEXT, tool TEXT, args TEXT,
                                   tainted INTEGER, status TEXT DEFAULT 'pending', expires TEXT, decided_ts TEXT);
            CREATE TABLE holds(agent TEXT PRIMARY KEY, ts TEXT, reason TEXT, action_id INTEGER);
            CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, agent TEXT, task TEXT,
                              schedule TEXT, next_run TEXT, last_run TEXT, last_status TEXT, enabled INTEGER DEFAULT 1);
        """)

    def audit(self, agent, tool, args=None, result="", status="ok", age=5):
        self.db.execute("INSERT INTO audit(ts, agent, tool, args, result, status) VALUES(?,?,?,?,?,?)",
                        (ago(age), agent, tool, json.dumps(args or {}), result, status))

    def query(self, sql, params=()):
        try:
            return self.db.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []

    def where(self):
        return {a["name"]: a for a in office.state(self.query)["agents"]}

    def test_places_follow_the_latest_tool(self):
        self.audit("writer", "write_file")
        self.audit("scholar", "search_memory")
        self.audit("coder", "run_python")
        self.audit("mcp", "github__list_issues")
        self.audit("sleepy", "read_file", age=600)
        self.audit("done", "read_file")
        self.audit("done", "(run)")
        self.audit("blocked", "write_file", status="denied")
        w = self.where()
        self.assertEqual({n: a["place"] for n, a in w.items()},
                         {"writer": "desk", "scholar": "library", "coder": "server", "mcp": "server",
                          "sleepy": "break", "done": "break", "blocked": "security"})
        self.assertEqual(w["writer"]["state"], "using write_file")

    def test_two_agents_talking_sit_in_the_lounge_with_what_they_said(self):
        self.audit("lead", "agent__editor", {"task": "Please tighten the intro"}, "Done: cut it to two lines")
        w = self.where()
        self.assertEqual((w["lead"]["place"], w["editor"]["place"]), ("talk", "talk"))
        self.assertEqual(w["lead"]["with"], ["editor"])
        self.assertEqual(w["lead"]["says"], "@editor Please tighten the intro")
        self.assertEqual(w["editor"]["says"], "Done: cut it to two lines")
        self.assertEqual(w["editor"]["state"], "talking with lead")

    def test_three_or_more_hold_a_discussion_in_the_meeting_room(self):
        self.audit("lead", "swarm", {"tasks": [{"helper": "agent__a", "task": "x"}, {"helper": "b", "task": "y"}]})
        self.audit("x", "agent__y", {"task": "hi"})
        self.audit("y", "agent__z", {"task": "hi"})
        w = self.where()
        for n in ("lead", "a", "b"):
            self.assertEqual(w[n]["place"], "meeting", n)
        self.assertEqual(w["lead"]["with"], ["a", "b"])
        for n in ("x", "y", "z"):  # a chain of hand-offs is one discussion too
            self.assertEqual(w[n]["place"], "meeting", n)
        self.assertEqual(w["x"]["group"], w["z"]["group"])

    def test_remote_agents_visit_as_guests_and_old_talks_end(self):
        self.audit("main", "a2a__partner", {"task": "quote?"}, "42")
        self.audit("old", "agent__gone", {"task": "x"}, age=office.TALK_S + 60)
        w = self.where()
        self.assertEqual(w["partner (remote)"]["kind"], "guest")
        self.assertEqual(w["partner (remote)"]["place"], "talk")
        self.assertEqual(w["old"]["place"], "break")
        self.assertNotIn("gone", w)

    def test_clipped_audit_arguments_still_give_the_message(self):
        self.db.execute("INSERT INTO audit(ts, agent, tool, args, result, status) VALUES(?,?,?,?,?,?)",
                        (ago(3), "lead", "agent__editor", '{"task": "a very long ...(truncated)', "", "ok"))
        self.assertEqual(self.where()["lead"]["says"], "@editor a very long ...(truncated)")

    def test_approvals_holds_and_due_jobs(self):
        self.audit("waiter", "read_file")
        self.db.execute("INSERT INTO approvals(ts, agent, tool, expires) VALUES(?,?,?,?)",
                        (ago(1), "waiter", "write_file", ago(-300)))
        self.db.execute("INSERT INTO approvals(ts, agent, tool, expires) VALUES(?,?,?,?)",
                        (ago(900), "expired", "write_file", ago(60)))
        self.db.execute("INSERT INTO holds VALUES('suspect', ?, 'too many refusals', 1)", (ago(1),))
        self.db.execute("INSERT INTO jobs(name, agent, task, schedule, next_run) VALUES('digest','r','t','daily',?)",
                        (ago(-120),))
        self.db.execute("INSERT INTO jobs(name, agent, task, schedule, next_run) VALUES('later','r','t','daily',?)",
                        (ago(-7200),))
        st = office.state(self.query)
        w = {a["name"]: a for a in st["agents"]}
        self.assertTrue(st["waiting_for_you"])
        self.assertEqual(w["waiter"]["place"], "approval")
        self.assertNotIn("expired", w)
        self.assertEqual(w["suspect"]["place"], "security")
        self.assertIn("too many refusals", w["suspect"]["state"])
        self.assertEqual(w["job-digest"]["place"], "waiting")
        self.assertEqual(w["job-later"]["place"], "break")
        self.assertEqual(st["counts"]["approval"], 1)

    def test_busy_agents_are_drawn_first_and_the_crowd_is_capped(self):
        for i in range(office.MAX_AGENTS + 10):
            self.audit(f"idle{i:03}", "now", age=900)
        self.audit("zz-busy", "now")
        names = [a["name"] for a in office.state(self.query)["agents"]]
        self.assertEqual(len(names), office.MAX_AGENTS)
        self.assertEqual(names[0], "zz-busy")

    def test_empty_or_old_database(self):
        st = office.state(lambda sql, p=(): [])
        self.assertEqual((st["agents"], st["waiting_for_you"]), ([], False))


class OfficePageTests(ConsoleCase):
    def test_office_page_allows_only_its_own_script(self):
        r, body = self.req("/office")
        self.assertEqual(r.status, 200)
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertEqual(re.findall(r"<script[^>]*>", body), ['<script src="/office.js">'])
        self.assertNotIn("<img", body)
        r, _ = self.req("/audit")  # other pages still forbid scripts
        self.assertNotIn("script-src", r.getheader("Content-Security-Policy"))

    def test_script_never_builds_html(self):
        r, js = self.req("/office.js")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("text/javascript"))
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "Function("):
            self.assertNotIn(bad, js)

    def test_json_needs_the_cookie_and_carries_attacker_text_only_as_data(self):
        r, _ = self.req("/office.json", cookie=False)
        self.assertEqual(r.status, 403)
        st = aios.Store(str(self.home / "aios.db"))
        st.log_audit("lead", "agent__main", {"task": XSS}, XSS, "ok")
        st.db.close()
        r, body = self.req("/office.json")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.getheader("Content-Type").startswith("application/json"))
        w = {a["name"]: a for a in json.loads(body)["agents"]}
        self.assertEqual(w["lead"]["place"], "talk")
        self.assertIn("<script>", w["lead"]["says"])  # data for fillText/textContent, not markup
        _, page = self.req("/office")
        self.assertNotIn("<script>alert", page)  # the no-script table escapes it

    def test_nav_links_the_office(self):
        _, body = self.req("/")
        self.assertIn('href="/office"', body)


if __name__ == "__main__":
    unittest.main()
