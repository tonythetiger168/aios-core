"""Local web console: access control, read-only behaviour and escaping of attacker-written text."""
import http.client
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_console  # noqa: E402

TOKEN = "test-token-123"
XSS = '<script>alert(1)</script><img src=x onerror=alert(2)>'


class ConsoleCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        st = aios.Store(str(self.home / "aios.db"))
        st.log_audit("main", "read_file", {"path": "inbox.txt"}, f"mail says {XSS}", "ok")
        st.log_audit("main", "write_file", {"path": "x.txt", "content": XSS}, "permission denied: nope", "denied")
        st.log_audit("other", "now", {}, "2026-10-21", "ok")
        st.set_fact("main", "owner", "alice", "user")
        st.set_fact("main", "rule", XSS, "tainted")
        st.add_job("digest", "reporter", "summarise", "daily 09:00", "2026-10-22T09:00:00")
        st.add_run("digest", "2026-10-21T09:00:00", "ok", f"done {XSS}")
        st.pin_skill("meeting-notes", "ab" * 32, "1.0")
        st.db.close()
        self.srv, self.url = aios_console.start(str(self.home), port=0, token=TOKEN)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]

    def req(self, path, method="GET", host=None, cookie=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if cookie:
            headers["Cookie"] = f"{aios_console.COOKIE}={TOKEN}"
        c.request(method, path, headers=headers)
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r, body


class AccessTests(ConsoleCase):
    def test_printed_url_points_at_localhost_with_the_token(self):
        self.assertEqual(self.url, f"http://127.0.0.1:{self.port}/?t={TOKEN}")
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_without_the_cookie_or_token_nothing_is_shown(self):
        for path in ("/", "/audit", "/memory"):
            r, body = self.req(path, cookie=False)
            self.assertEqual(r.status, 403)
            self.assertNotIn("alice", body)

    def test_the_token_is_traded_for_a_strict_cookie_and_removed_from_the_url(self):
        r, _ = self.req(f"/audit?agent=main&t={TOKEN}", cookie=False)
        self.assertEqual(r.status, 303)
        self.assertEqual(r.getheader("Location"), "/audit?agent=main")
        cookie = r.getheader("Set-Cookie")
        for part in (f"{aios_console.COOKIE}={TOKEN}", "HttpOnly", "SameSite=Strict"):
            self.assertIn(part, cookie)
        r, _ = self.req("/?t=wrong", cookie=False)
        self.assertEqual(r.status, 403)

    def test_a_wrong_cookie_is_refused(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}", "Cookie": f"{aios_console.COOKIE}=guess"})
        self.assertEqual(c.getresponse().status, 403)
        c.close()

    def test_foreign_host_headers_are_refused(self):  # DNS rebinding
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1:1", None):
            with self.subTest(host=host):
                r, body = self.req("/", host=host or f"127.0.0.1.evil.example:{self.port}")
                self.assertEqual(r.status, 403)
        r, _ = self.req("/", host=f"localhost:{self.port}")
        self.assertEqual(r.status, 200)

    def test_only_reads_are_allowed(self):
        for method in ("POST", "PUT", "DELETE"):
            r, _ = self.req("/audit", method=method)
            self.assertEqual(r.status, 405)
        r, body = self.req("/", method="HEAD")
        self.assertEqual((r.status, body), (200, ""))

    def test_security_headers(self):
        r, _ = self.req("/")
        self.assertIn("default-src 'none'", r.getheader("Content-Security-Policy"))
        self.assertNotIn("script-src", r.getheader("Content-Security-Policy"))  # no scripts at all
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertEqual(r.getheader("Referrer-Policy"), "no-referrer")


class PageTests(ConsoleCase):
    def test_every_page_renders_and_escapes_attacker_text(self):
        for path in ("/", "/audit", "/jobs", "/memory?agent=main", "/skills"):
            with self.subTest(path=path):
                r, body = self.req(path)
                self.assertEqual(r.status, 200)
                self.assertNotIn("<script", body)
                self.assertNotIn("<img", body)
        _, body = self.req("/audit")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)

    def test_overview_counts_and_refusals(self):
        _, body = self.req("/")
        self.assertIn("Refused", body)
        self.assertIn("write_file", body)  # the refused call is listed
        self.assertIn("permission denied: nope", body)

    def test_audit_filters(self):
        _, body = self.req("/audit?agent=other")
        self.assertIn("2026-10-21", body)
        self.assertNotIn("inbox.txt", body)
        _, body = self.req("/audit?status=denied")
        self.assertIn("x.txt", body)
        self.assertNotIn("inbox.txt", body)
        _, body = self.req("/audit?n=junk&tool=%27%20OR%201%3D1--")  # junk is harmless (parameterised SQL)
        self.assertIn("No matching entries", body)

    def test_memory_shows_who_wrote_each_note(self):
        _, body = self.req("/memory?agent=main")
        self.assertIn("alice", body)
        self.assertIn("tainted", body)
        self.assertIn("user", body)

    def test_jobs_and_skills(self):
        _, body = self.req("/jobs")
        self.assertIn("digest", body)
        self.assertIn("daily 09:00", body)
        _, body = self.req("/skills")
        self.assertIn("meeting-notes", body)
        self.assertIn("ab" * 32, body)

    def test_long_values_are_clipped(self):
        st = aios.Store(str(self.home / "aios.db"))
        st.log_audit("main", "now", {}, "y" * 1500, "ok")
        st.db.close()
        _, body = self.req("/audit")
        self.assertIn("(+", body)

    def test_unknown_pages_404(self):
        self.assertEqual(self.req("/admin")[0].status, 404)

    def test_the_database_is_opened_read_only(self):
        con = aios_console.Reader(self.home / "aios.db").connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DELETE FROM audit")
        finally:
            con.close()


class MissingDatabaseTests(unittest.TestCase):
    def test_no_database_is_created(self):
        with tempfile.TemporaryDirectory() as d:
            srv, _ = aios_console.start(d, port=0, token=TOKEN)
            try:
                port = srv.server_address[1]
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                c.request("GET", "/audit", headers={"Host": f"127.0.0.1:{port}",
                                                    "Cookie": f"{aios_console.COOKIE}={TOKEN}"})
                r = c.getresponse()
                self.assertIn("No database yet", r.read().decode())
                c.close()
            finally:
                srv.shutdown()
                srv.server_close()
            self.assertFalse(Path(d, "aios.db").exists())


if __name__ == "__main__":
    unittest.main()
