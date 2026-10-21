"""Browser approvals: unattended runs ask a person through `aios console --approve`."""
import asyncio
import http.client
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_console  # noqa: E402

TOKEN = "test-token-123"
XSS = '<script>alert(1)</script>'


class Home(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.db = self.home / "aios.db"
        self.store = aios.Store(str(self.db))
        self.addCleanup(self.store.db.close)


class StoreTests(Home):
    def test_a_request_starts_pending_and_expiry_keeps_a_decision(self):
        aid = self.store.request_approval("main", "write_file", {"path": "a.txt"}, False, 60)
        self.assertEqual(self.store.approval_status(aid), "pending")
        self.assertTrue(aios_console.decide(self.db, aid, True))
        self.store.expire_approval(aid)
        self.assertEqual(self.store.approval_status(aid), "approved")
        other = self.store.request_approval("main", "write_file", {}, False, 60)
        self.store.expire_approval(other)
        self.assertEqual(self.store.approval_status(other), "expired")

    def test_only_open_requests_can_be_answered(self):
        aid = self.store.request_approval("main", "run_python", {}, False, 60)
        self.assertTrue(aios_console.decide(self.db, aid, False))
        self.assertFalse(aios_console.decide(self.db, aid, True))  # already answered
        self.assertEqual(self.store.approval_status(aid), "denied")
        old = self.store.request_approval("main", "run_python", {}, False, -1)  # past its deadline
        self.assertFalse(aios_console.decide(self.db, old, True))
        self.assertFalse(aios_console.decide(self.db, 999, True))


class ApproverTests(Home, unittest.IsolatedAsyncioTestCase):
    async def answer(self, approve: bool, delay: float = 0.05):
        await asyncio.sleep(delay)
        rows = self.store.db.execute("SELECT id FROM approvals WHERE status='pending'").fetchall()
        for (aid,) in rows:
            aios_console.decide(self.db, aid, approve)

    async def test_waits_for_the_answer(self):
        ap = aios.ConsoleApprover(self.store, timeout=5, poll=0.01)
        for approve in (True, False):
            with self.subTest(approve=approve):
                answer = asyncio.create_task(self.answer(approve))
                self.assertIs(await ap("main", "write_file", {"path": "a"}, tainted=False), approve)
                await answer

    async def test_no_answer_in_time_means_no(self):
        ap = aios.ConsoleApprover(self.store, timeout=0.05, poll=0.01)
        self.assertFalse(await ap("main", "write_file", {}))
        (st,), = self.store.db.execute("SELECT status FROM approvals").fetchall()
        self.assertEqual(st, "expired")

    async def test_a_cancelled_run_closes_its_request(self):
        ap = aios.ConsoleApprover(self.store, timeout=5, poll=0.01)
        t = asyncio.create_task(ap("main", "write_file", {}))
        await asyncio.sleep(0.05)
        t.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t
        (st,), = self.store.db.execute("SELECT status FROM approvals").fetchall()
        self.assertEqual(st, "expired")  # nobody can approve it after the run is gone

    async def test_the_kernel_runs_the_tool_only_after_approval_and_records_taint(self):
        k = aios.Kernel(store=self.store, approver=aios.ConsoleApprover(self.store, timeout=5, poll=0.01),
                        provider=object())
        ran = []

        @k.tools.register("risky", "a risky tool", {"x": {"type": "string"}})
        async def risky(x=""):
            ran.append(x)
            return "done"

        k.needs_approval.add("risky")
        agent = aios.Agent("main", {"risky"})
        answer = asyncio.create_task(self.answer(True))
        out, err, st = await k._execute(agent, "risky", {"x": "1"}, True, {})
        await answer
        self.assertEqual((out, err, st, ran), ("done", False, "ok", ["1"]))
        (tainted,), = self.store.db.execute("SELECT tainted FROM approvals").fetchall()
        self.assertEqual(tainted, 1)  # the console warns the person
        answer = asyncio.create_task(self.answer(False))
        out, err, st = await k._execute(agent, "risky", {"x": "2"}, False, {})
        await answer
        self.assertEqual((st, ran), ("denied", ["1"]))
        self.assertIn("not approved by a human", out)

    async def test_serve_gateway_drops_the_pre_allow_hint_when_a_person_can_answer(self):
        k = aios.Kernel(store=self.store, approver=aios.ConsoleApprover(self.store, timeout=0.05, poll=0.01))
        k.tools.register("risky", "a risky tool", {})(lambda: "done")
        k.needs_approval.add("risky")
        out, err = await aios.Gateway(k, aios.Agent("mcp", {"risky"})).call("risky", {})
        self.assertTrue(err)
        self.assertNotIn("auto_paths", out)


class ConsoleCase(Home):
    approve = True

    def setUp(self):
        super().setUp()
        self.aid = self.store.request_approval("main", "write_file", {"path": "x.txt", "content": XSS}, True, 600)
        self.srv, _ = aios_console.start(str(self.home), port=0, token=TOKEN, approve=self.approve)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def req(self, path, method="GET", body=None, origin=True, cookie=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if cookie:
            headers["Cookie"] = f"{aios_console.COOKIE}={TOKEN}"
        if origin:
            headers["Origin"] = self.origin if origin is True else origin
        data = None
        if body is not None:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        return r, text

    def form(self, decision="approve", **kw):
        if not hasattr(self, "csrf"):  # read from the page, as a browser would
            _, page = self.req("/approvals")
            self.csrf = page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
        return dict({"id": str(self.aid), "csrf": self.csrf, "decision": decision}, **kw)


class ConsoleApprovalTests(ConsoleCase):
    def test_pending_request_is_shown_escaped_with_the_taint_warning(self):
        r, body = self.req("/approvals")
        self.assertEqual(r.status, 200)
        self.assertIn("write_file", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script", body)
        self.assertIn("untrusted content", body)
        self.assertIn('value="approve"', body)
        self.assertIn("approvals on", body)
        _, overview = self.req("/")
        self.assertIn("Waiting for approval", overview)

    def test_approve_and_deny(self):
        r, _ = self.req("/approvals", "POST", self.form())
        self.assertEqual((r.status, r.getheader("Location")), (303, "/approvals?r=approved"))
        self.assertEqual(self.store.approval_status(self.aid), "approved")
        r, _ = self.req("/approvals", "POST", self.form("deny"))  # too late: already answered
        self.assertEqual(r.getheader("Location"), "/approvals?r=gone")
        self.assertEqual(self.store.approval_status(self.aid), "approved")
        other = self.store.request_approval("main", "run_python", {}, False, 600)
        self.req("/approvals", "POST", self.form("deny", id=str(other)))
        self.assertEqual(self.store.approval_status(other), "denied")

    def test_forged_submissions_are_refused(self):
        cases = {
            "no Origin": dict(origin=False),
            "foreign Origin": dict(origin="http://evil.example"),
            "null Origin": dict(origin="null"),
            "no cookie": dict(cookie=False),
        }
        good = self.form()
        for name, kw in cases.items():
            with self.subTest(name):
                r, _ = self.req("/approvals", "POST", good, **kw)
                self.assertEqual(r.status, 403)
        for name, body in {"wrong form token": dict(good, csrf="x" * 64),
                           "missing form token": {k: v for k, v in good.items() if k != "csrf"}}.items():
            with self.subTest(name):
                self.assertEqual(self.req("/approvals", "POST", body)[0].status, 403)
        for body in (dict(good, decision="maybe"), dict(good, id="1 OR 1=1")):
            self.assertEqual(self.req("/approvals", "POST", body)[0].status, 400)
        self.assertEqual(self.req("/approvals", "POST", dict(good, pad="x" * 5000))[0].status, 413)
        self.assertEqual(self.store.approval_status(self.aid), "pending")

    def test_other_writes_stay_closed(self):
        for path, method in (("/audit", "POST"), ("/approvals", "PUT"), ("/approvals", "DELETE")):
            with self.subTest(path=path, method=method):
                self.assertEqual(self.req(path, method, self.form())[0].status, 405)

    def test_expired_requests_move_to_answered(self):
        self.store.expire_approval(self.aid)
        _, body = self.req("/approvals")
        self.assertIn("Nothing is waiting", body)
        self.assertIn('http-equiv="refresh"', body)  # reloads while idle
        self.assertIn("expired", body)


class ReadOnlyConsoleTests(ConsoleCase):
    approve = False

    def test_requests_are_visible_but_cannot_be_answered(self):
        _, body = self.req("/approvals")
        self.assertIn("write_file", body)
        self.assertNotIn('name="csrf"', body)
        self.assertIn("--approve", body)
        r, _ = self.req("/approvals", "POST", {"id": str(self.aid), "decision": "approve", "csrf": ""})
        self.assertEqual(r.status, 405)
        self.assertEqual(self.store.approval_status(self.aid), "pending")


if __name__ == "__main__":
    unittest.main()
