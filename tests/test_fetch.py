"""The fetch_url tool: a real HTTP server on 127.0.0.1 (allowed only with allow_private), no network."""
import asyncio
import http.server
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_fetch as f  # noqa: E402

PAGES = {
    "/page": ("text/html; charset=utf-8", b"<html><head><title>Hello  page</title><style>p{}</style>"
              b"<script>steal()</script></head><body><h1>Title</h1><p>One &amp; two</p>"
              b"<p>three</p></body></html>"),
    "/big": ("text/plain", b"x" * 5000),
    "/bin": ("image/png", b"\x89PNG" + b"\0" * 100),
    "/json": ("application/json", b'{"a": 1}'),
    "/big5": ("text/plain; charset=big5", "中文".encode("big5")),
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/go"):
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        ctype, body = PAGES.get(self.path, ("text/plain", b"missing"))
        self.send_response(200 if self.path in PAGES else 404)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):  # never through a proxy configured on the machine running the tests
        p = mock.patch.object(f.urllib.request, "getproxies", lambda: {})
        p.start()
        self.addCleanup(p.stop)

    def get(self, path, **kw):
        kw.setdefault("allow_private", True)
        return asyncio.run(f.fetch(self.base + path, **kw))

    def test_html_becomes_text_without_scripts_or_styles(self):
        out = self.get("/page")
        self.assertTrue(out.startswith(f"HTTP 200 {self.base}/page\ncontent-type: text/html"))
        self.assertIn("# Hello page", out)
        self.assertIn("Title\nOne & two\nthree", out)
        self.assertNotIn("steal", out)
        self.assertNotIn("p{}", out)

    def test_redirects_are_reported_not_followed(self):
        out = self.get("/go")
        self.assertIn("HTTP 302", out)
        self.assertIn(f"redirected to {self.base}/page", out)
        self.assertNotIn("Title", out)

    def test_size_cap_binary_json_charset_and_errors(self):
        out = self.get("/big", max_bytes=1000)
        self.assertIn("x" * 1000 + "\n...(truncated after 1000 bytes)", out)
        self.assertNotIn("x" * 1001, out)
        self.assertIn("(binary content, 104 bytes: not shown)", self.get("/bin"))
        self.assertIn('{"a": 1}', self.get("/json"))
        self.assertIn("中文", self.get("/big5"))
        self.assertIn("HTTP 404", self.get("/nothing"))

    def test_local_and_private_addresses_are_refused_by_default(self):
        for url in (self.base + "/page", "http://localhost/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
                    "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/", "http://0.0.0.0/"):
            with self.subTest(url=url), self.assertRaisesRegex(f.FetchError, "not a public address"):
                asyncio.run(f.fetch(url))

    def test_a_name_must_resolve_only_to_public_addresses_and_the_checked_one_is_used(self):
        def fake(addrs):
            return lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addrs]

        with mock.patch.object(f.socket, "getaddrinfo", fake(["93.184.215.14", "192.168.1.1"])):
            with self.assertRaisesRegex(f.FetchError, "not a public address"):
                asyncio.run(f.fetch("http://rebind.example/"))
        connected = []

        def no_connect(addr, timeout=None):
            connected.append(addr)
            raise OSError("stop here")

        with mock.patch.object(f.socket, "getaddrinfo", fake(["93.184.215.14"])), \
                mock.patch.object(f.socket, "create_connection", no_connect):
            with self.assertRaises(f.FetchError):
                asyncio.run(f.fetch("http://public.example/"))
        self.assertEqual(connected, [("93.184.215.14", 80)])

    def test_bad_urls_are_refused(self):
        for url, msg in (("file:///etc/passwd", "only http"), ("ftp://example.com/", "only http"),
                         ("http://user:pw@example.com/", "user name"), ("http:///x", "no host"),
                         ("http://example.com:99999/", "bad port")):
            with self.subTest(url=url), self.assertRaisesRegex(f.FetchError, msg):
                asyncio.run(f.fetch(url))


class Scripted:
    def __init__(self, *calls):
        self.calls = list(calls)

    async def complete(self, system, messages, tools, max_tokens=1024):
        if self.calls:
            url = self.calls.pop(0)
            return aios.Completion([{"type": "tool_use", "id": url, "name": "fetch_url", "input": {"url": url}}],
                                   "tool_use")
        return aios.Completion([{"type": "text", "text": "done"}], "end_turn")


class FetchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_needs_approval_taints_the_run_and_a_tainted_run_cannot_auto_fetch(self):
        FetchTests.setUpClass()
        self.addCleanup(FetchTests.tearDownClass)
        base = FetchTests.base
        p = mock.patch.object(f.urllib.request, "getproxies", lambda: {})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(setattr, aios, "WORKSPACE", aios.WORKSPACE)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        aios.WORKSPACE = Path(tmp.name)
        with mock.patch.dict(os.environ, {"AIOS_FETCH_ALLOW_PRIVATE": "1"}):
            k = aios.Kernel(store=aios.Store(":memory:"), provider=Scripted(base + "/page", base + "/json"))
            aios.install_builtin_tools(k)
            self.assertIn("fetch_url", k.needs_approval)
            self.assertTrue(k.tools.is_untrusted("fetch_url"))
            self.assertNotIn("fetch_url", aios.DEFAULT_TOOLS)  # granted only on purpose
            scopes = {"fetch_url": {"auto_args": {"url": [base + "/*"]}}}
            await k._run(aios.Agent("a", {"fetch_url"}, scopes=scopes), "read the page")
        log = [(e["tool"], e["status"]) for e in k.store.audit_tail("a", 10) if e["tool"] == "fetch_url"]
        # the first page is outside data: the next fetch could carry it out, so it needs a person
        self.assertEqual(log, [("fetch_url", "ok"), ("fetch_url", "denied")])
        k.store.db.close()


if __name__ == "__main__":
    unittest.main()
