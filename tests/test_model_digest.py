"""Pinned local model weights: a model whose digest changed is refused before any call."""
import asyncio
import contextlib
import http.server
import io
import json
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402

GOOD = "a" * 64
CALLS = []


class FakeOllama(http.server.BaseHTTPRequestHandler):
    digest = GOOD

    def log_message(self, *a):
        pass

    def _send(self, body: dict):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send({"models": [{"name": "gemma4:12b", "model": "gemma4:12b", "digest": self.digest},
                                   {"name": "tiny:latest", "model": "tiny:latest", "digest": "b" * 64}]})
        else:
            self.send_error(404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        CALLS.append(self.path)
        self._send({"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}})


class DigestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        FakeOllama.digest = GOOD
        CALLS.clear()

    def call(self, p):
        return asyncio.run(p.complete("sys", [{"role": "user", "content": "x"}], []))

    def test_lists_models_and_finds_latest(self):
        self.assertEqual(aios.ollama_models(self.url)["gemma4:12b"], GOOD)
        self.assertEqual(aios.ollama_digest(self.url, "tiny"), "b" * 64)
        self.assertIsNone(aios.ollama_models("http://127.0.0.1:1", timeout=1))

    def test_matching_prefix_runs(self):
        p = aios.OpenAICompatProvider("gemma4:12b", self.url, digest="sha256:" + GOOD[:12].upper())
        self.assertEqual(self.call(p).stop_reason, "end_turn")
        self.assertEqual(CALLS, ["/v1/chat/completions"])

    def test_swapped_model_is_refused_before_any_call(self):
        FakeOllama.digest = "c" * 64
        p = aios.OpenAICompatProvider("gemma4:12b", self.url, digest=GOOD[:12])
        with self.assertRaisesRegex(RuntimeError, "re-pulled or replaced"):
            self.call(p)
        self.assertEqual(CALLS, [])

    def test_first_call_is_checked_even_soon_after_boot(self):
        # time.monotonic() counts from boot on Windows: a fresh machine must not skip the first check
        FakeOllama.digest = "c" * 64
        p = aios.OpenAICompatProvider("gemma4:12b", self.url, digest=GOOD[:12])
        with mock.patch.object(aios.time, "monotonic", return_value=5.0), self.assertRaises(RuntimeError):
            self.call(p)
        self.assertEqual(CALLS, [])

    def test_unknown_model_or_no_tags_is_refused(self):
        p = aios.OpenAICompatProvider("missing:1b", self.url, digest=GOOD[:12])
        with self.assertRaisesRegex(RuntimeError, "cannot check the digest"):
            self.call(p)
        self.assertEqual(CALLS, [])

    def test_recheck_after_the_interval(self):
        p = aios.OpenAICompatProvider("gemma4:12b", self.url, digest=GOOD[:12])
        self.call(p)
        FakeOllama.digest = "c" * 64
        self.call(p)  # within DIGEST_RECHECK: not asked again
        p._digest_checked -= aios.DIGEST_RECHECK + 1
        with self.assertRaises(RuntimeError):
            self.call(p)

    def test_bad_digest_text_and_wrong_provider(self):
        with self.assertRaises(ValueError):
            aios.normalize_digest("not-hex")
        with self.assertRaises(ValueError):
            aios.normalize_digest("abc")  # too short to mean anything
        ap = aios.argparse.ArgumentParser()
        aios.add_model_flags(ap)
        a = ap.parse_args(["--model-digest", GOOD[:12]])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            aios.build_model(a, ap)


if __name__ == "__main__":
    unittest.main()
