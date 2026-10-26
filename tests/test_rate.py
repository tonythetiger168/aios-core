"""Per-agent sliding-window rate limits on tool and model calls (policy `[agents.X.rate]`)."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402


class Scripted:
    def __init__(self, *script):
        self.script, self.calls = list(script), 0

    async def complete(self, system, messages, tools, max_tokens=1024):
        self.calls += 1
        return self.script.pop(0) if self.script else text("done")


def text(t):
    return aios.Completion([{"type": "text", "text": t}], "end_turn", 0, 0)


def call(name, args, id="c1"):
    return aios.Completion([{"type": "tool_use", "id": id, "name": name, "input": args}], "tool_use", 0, 0)


class ParseTests(unittest.TestCase):
    def test_limits_read_like_people_write_them(self):
        cases = {"10/min": (10, 60), "10/mins": (10, 60), "200/hour": (200, 3600), "5/30s": (5, 30),
                 "2/5min": (2, 300), "1000/day": (1000, 86400), " 3 / 2 hours ": (3, 7200)}
        for t, want in cases.items():
            self.assertEqual(aios.parse_limit(t, "x"), want, t)

    def test_typos_fail_loudly(self):
        for bad in ("5/ms", "0/min", "5/0min", "5/week", "5/2day", "ten/min", 5, ""):
            with self.assertRaises(ValueError, msg=repr(bad)):
                aios.parse_limit(bad, "x")
        with self.assertRaises(ValueError):
            aios.Agent("a", set(), rate={"modle": "1/min"})
        with self.assertRaises(ValueError):
            aios.Agent("a", set(), rate={"tools": ["fetch_url"]})

    def test_policy_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "p.toml")
            p.write_text('[agents.r]\npermissions = ["now"]\n[agents.r.rate]\nmodel = "30/min"\n'
                         '[agents.r.rate.tools]\n"*" = "100/hour"\nnow = "2/min"\n', encoding="utf-8")
            pol = aios.load_policy(str(p))
            self.assertEqual(pol["r"]["rate"], {"model": "30/min", "tools": {"*": "100/hour", "now": "2/min"}})
            p.write_text('[agents.r.rate]\nmodel = "30/fortnight"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "agents.r: rate.model"):
                aios.load_policy(str(p))

    def test_windows_print_plainly(self):
        self.assertEqual([aios.fmt_window(s) for s in (60, 300, 3600, 7200, 86400, 30)],
                         ["min", "5 min", "hour", "2 hour", "day", "30s"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = str(Path(self._tmp.name, "aios.db"))
        self.st = aios.Store(self.path)
        self.addCleanup(self.st.db.close)

    def test_the_window_slides(self):
        rule = [("model", 2, 60.0)]
        self.assertIsNone(self.st.rate_take("a", rule, now=100))
        self.assertIsNone(self.st.rate_take("a", rule, now=130))
        full = self.st.rate_take("a", rule, now=150)
        self.assertEqual((full["rule"], full["calls"], full["retry"]), ("model", 2, 10))
        self.assertIsNone(self.st.rate_take("a", rule, now=160.5))  # the call at 100 left the window
        self.assertIsNotNone(self.st.rate_take("a", rule, now=170))
        self.assertIsNone(self.st.rate_take("b", rule, now=170))  # per agent

    def test_all_or_nothing_across_rules(self):
        rules = [("tool:*", 5, 60.0), ("tool:fetch_url", 1, 60.0)]
        self.assertIsNone(self.st.rate_take("a", rules, now=0))
        self.assertEqual(self.st.rate_take("a", rules, now=1)["rule"], "tool:fetch_url")
        # the refused call was not counted against '*'
        n = self.st.db.execute("SELECT COUNT(*) FROM rate_hits WHERE rule='tool:*'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_shared_by_every_process_on_the_database(self):
        rule = [("model", 1, 3600.0)]
        self.assertIsNone(self.st.rate_take("a", rule))
        other = aios.Store(self.path)
        try:
            self.assertIsNotNone(other.rate_take("a", rule))
        finally:
            other.db.close()


class KernelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = aios.WORKSPACE
        aios.WORKSPACE = Path(self._tmp.name).resolve()
        self.addCleanup(setattr, aios, "WORKSPACE", self._old)

    def kernel(self, *script):
        k = aios.Kernel(store=aios.Store(":memory:"), provider=Scripted(*script))
        aios.install_builtin_tools(k)
        self.addCleanup(k.store.db.close)
        return k

    def statuses(self, k):
        return [e["status"] for e in k.store.audit_tail(n=100) if e["tool"] == "now"]

    async def test_tool_calls_over_the_limit_are_refused_across_runs(self):
        k = self.kernel(call("now", {}, "a"), call("now", {}, "b"), text("ok"),
                        call("now", {}, "c"), text("ok"))
        agent = aios.Agent("r", {"now"}, rate={"tools": {"now": "2/hour"}})
        await k._run(agent, "go")
        await k._run(agent, "again")  # a new run does not reset it (unlike max_calls)
        self.assertEqual(self.statuses(k), ["ok", "ok", "denied"])
        last = [e for e in k.store.audit_tail(n=100) if e["tool"] == "now"][-1]["result"]
        self.assertIn("rate limit for r: now at most 2 per hour", last)

    async def test_jobs_share_their_policy_agents_limits(self):
        k = self.kernel(call("now", {}, "a"), text("ok"), call("now", {}, "b"), text("ok"))
        pol = {"reporter": {"permissions": {"now"}, "scopes": {}, "rate": {"tools": {"*": "1/day"}}}}
        sched = aios.Scheduler(k, pol, None, 10, 60)
        for name in ("one", "two"):
            await k._run(sched.agent_for({"name": name, "agent": "reporter"}), "go")
        self.assertEqual(self.statuses(k), ["ok", "denied"])

    async def test_model_calls_wait_briefly_then_stop_the_run(self):
        k = self.kernel(call("now", {}, "a"), text("ok"))
        agent = aios.Agent("r", {"now"}, rate={"model": "1/hour"})
        answer = await k._run(agent, "go")
        self.assertIn("stopped: rate limit for r: at most 1 model calls per hour", answer)
        self.assertEqual(k.provider.calls, 1)
        run = [e for e in k.store.audit_tail(n=100) if e["tool"] == aios.RUN_ENTRY][-1]
        self.assertEqual(run["status"], "rate")

    async def test_a_short_wait_is_waited_out(self):
        k = self.kernel(call("now", {}, "a"), text("ok"))
        agent = aios.Agent("r", {"now"}, rate={"model": "1/s"})
        sleeps = []
        real_sleep = asyncio.sleep

        async def fast_sleep(s):
            sleeps.append(s)
            k.store.db.execute("UPDATE rate_hits SET ts = ts - 2")  # time passes
            await real_sleep(0)
        with mock.patch.object(aios.asyncio, "sleep", fast_sleep):
            answer = await k._run(agent, "go")
        self.assertEqual(answer, "ok")
        self.assertEqual(k.provider.calls, 2)
        self.assertTrue(sleeps and sleeps[0] <= 1.1)


if __name__ == "__main__":
    unittest.main()
