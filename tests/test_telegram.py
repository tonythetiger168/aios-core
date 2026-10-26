"""Telegram approvals (aios_telegram): requests in aios.db answered with buttons in a chat."""
import asyncio
import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_telegram as tg  # noqa: E402

ME, STRANGER = 111, 999


class FakeAPI:
    """Records Bot API calls; getUpdates hands out the queued updates once."""

    def __init__(self):
        self.calls, self.updates, self.next_id = [], [], 1

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        if method == "getUpdates":
            out, self.updates = [u for u in self.updates if u["update_id"] >= params["offset"]], []
            return out
        if method == "getMe":
            return {"username": "aios_bot"}
        return True

    def sent(self, method):
        return [p for m, p in self.calls if m == method]

    def press(self, data, user=ME, chat=ME, message_id=2):
        uid = len(self.calls) + 100
        self.updates.append({"update_id": uid, "callback_query": {
            "id": f"q{uid}", "from": {"id": user}, "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat}, "text": "old"}}})


class BridgeTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = d.name
        self.store = aios.Store(os.path.join(d.name, "aios.db"))
        self.addCleanup(self.store.db.close)
        self.api = FakeAPI()
        self.logs = []
        self.bridge = tg.Bridge(self.home, ME, self.api, log=self.logs.append)

    def ask(self, tool="write_file", args=None, tainted=False, seconds=60):
        return self.store.request_approval("main", tool, args or {"path": "a.txt"}, tainted, seconds)

    def test_pending_request_is_sent_once_with_buttons(self):
        aid = self.ask(tainted=True)
        self.bridge.step(0)
        self.bridge.step(0)
        (msg,) = self.api.sent("sendMessage")
        self.assertEqual(msg["chat_id"], ME)
        self.assertNotIn("parse_mode", msg)  # plain text: arguments cannot reformat the message
        self.assertIn(f"#{aid}", msg["text"])
        self.assertIn("main wants to run write_file", msg["text"])
        self.assertIn('"path": "a.txt"', msg["text"])
        self.assertIn("untrusted content", msg["text"])
        buttons = msg["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["callback_data"] for b in buttons], [f"a:{aid}", f"d:{aid}"])

    def test_approve_and_deny(self):
        a1, a2 = self.ask(), self.ask("run_python", {"code": "print(1)"})
        self.bridge.step(0)
        self.api.press(f"a:{a1}")
        self.api.press(f"d:{a2}", message_id=3)
        self.bridge.step(0)
        self.assertEqual(self.store.approval_status(a1), "approved")
        self.assertEqual(self.store.approval_status(a2), "denied")
        edits = {p["message_id"]: p["text"] for p in self.api.sent("editMessageText")}
        self.assertIn("✅ Approved", edits[2])
        self.assertIn("❌ Denied", edits[3])
        self.assertEqual(self.bridge.sent, {})

    def test_strangers_cannot_answer(self):
        aid = self.ask()
        self.bridge.step(0)
        self.api.press(f"a:{aid}", user=STRANGER, chat=STRANGER)
        self.api.press(f"a:{aid}", user=STRANGER, chat=ME)  # someone else in a group chat
        self.bridge.step(0)
        self.assertEqual(self.store.approval_status(aid), "pending")
        self.assertEqual([p["text"] for p in self.api.sent("answerCallbackQuery")], ["Not allowed."] * 2)
        self.assertEqual(len([m for m in self.logs if "not allowed" in m]), 2)

    def test_allow_user_in_a_group(self):
        group = -100123
        bridge = tg.Bridge(self.home, group, self.api, allow_users={ME}, log=self.logs.append)
        aid = self.ask()
        bridge.step(0)
        self.api.press(f"a:{aid}", user=STRANGER, chat=group)
        bridge.step(0)
        self.assertEqual(self.store.approval_status(aid), "pending")
        self.api.press(f"a:{aid}", user=ME, chat=group)
        bridge.step(0)
        self.assertEqual(self.store.approval_status(aid), "approved")

    def test_expired_or_answered_elsewhere_is_shown(self):
        a1, a2 = self.ask(), self.ask()
        self.bridge.step(0)
        self.store.expire_approval(a1)  # nobody answered in time
        import aios_console
        aios_console.decide(Path(self.home, "aios.db"), a2, True)  # answered in the web console
        self.bridge.step(0)
        edits = [p["text"] for p in self.api.sent("editMessageText")]
        self.assertIn("⌛ Expired", edits[0])
        self.assertIn("✅ Approved", edits[1])
        self.api.press(f"d:{a2}")  # a late press changes nothing
        self.bridge.step(0)
        self.assertEqual(self.store.approval_status(a2), "approved")
        self.assertIn("Already answered or expired", self.api.sent("answerCallbackQuery")[-1]["text"])

    def test_long_arguments_are_clipped(self):
        self.ask(args={"content": "x" * 10_000})
        self.bridge.step(0)
        text = self.api.sent("sendMessage")[0]["text"]
        self.assertLessEqual(len(text), tg.TEXT_MAX)
        self.assertIn("see the console", text)

    def test_bad_callback_data_is_ignored(self):
        aid = self.ask()
        self.bridge.step(0)
        for data in ("x:1", "a:", "a:-1", "a:1;DROP"):
            self.api.press(data)
        self.bridge.step(0)
        self.assertEqual(self.store.approval_status(aid), "pending")

    def test_end_to_end_with_console_approver(self):
        """A run waiting in ConsoleApprover goes on once the button is pressed."""
        approver = aios.ConsoleApprover(self.store, timeout=10, poll=0.02)

        def phone():
            for _ in range(200):
                self.bridge.step(0)
                if self.api.sent("sendMessage"):
                    aid = int(self.api.sent("sendMessage")[0]["reply_markup"]["inline_keyboard"][0][0]
                              ["callback_data"][2:])
                    self.api.press(f"a:{aid}")
                    self.bridge.step(0)
                    return
                threading.Event().wait(0.02)
        t = threading.Thread(target=phone)
        t.start()
        with contextlib.redirect_stderr(io.StringIO()):
            ok = asyncio.run(approver("main", "write_file", {"path": "x"}))
        t.join()
        self.assertTrue(ok)


class HttpApiTests(unittest.TestCase):
    def test_calls_bot_api_and_hides_token_in_errors(self):
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, body))
                ok = self.path.endswith("/getMe")
                out = json.dumps({"ok": True, "result": {"username": "b"}} if ok else
                                 {"ok": False, "description": "Bad Request: nope"}).encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}):
            api = tg.http_api("SECRET:TOKEN", f"http://127.0.0.1:{srv.server_port}")
            self.assertEqual(api("getMe", {}), {"username": "b"})
            with self.assertRaises(tg.TelegramError) as e:
                api("sendMessage", {"chat_id": 1, "text": "hi"})
        self.assertEqual(seen[0][0], "/botSECRET:TOKEN/getMe")
        self.assertEqual(seen[1][1]["text"], "hi")
        self.assertIn("Bad Request: nope", str(e.exception))
        self.assertNotIn("SECRET", str(e.exception))
        with self.assertRaises(tg.TelegramError) as e:
            tg.http_api("SECRET:TOKEN", "http://127.0.0.1:1", timeout=2)("getMe", {})
        self.assertNotIn("SECRET", str(e.exception))


class CliTests(unittest.TestCase):
    def test_needs_db_token_and_chat(self):
        with tempfile.TemporaryDirectory() as d:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(tg.run(d, ME, api=FakeAPI()), 1)
            self.assertIn("no aios.db", err.getvalue())
            aios.Store(os.path.join(d, "aios.db")).db.close()
            env = {k: v for k, v in os.environ.items() if k != tg.TOKEN_ENV}
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(err):
                self.assertEqual(tg.run(d, ME), 1)
            self.assertIn(tg.TOKEN_ENV, err.getvalue())

            def broken(method, params):
                raise tg.TelegramError("getMe: Unauthorized")
            with contextlib.redirect_stderr(err):
                self.assertEqual(tg.run(d, ME, api=broken), 1)
            self.assertIn("Unauthorized", err.getvalue())
        err = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "AIOS_TELEGRAM_CHAT"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", ["aios", "telegram"]), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            aios.cli()
        self.assertIn("--chat-id", err.getvalue())
        with mock.patch.dict(os.environ, {"AIOS_TELEGRAM_CHAT": "-100123"}), \
                mock.patch.object(sys, "argv", ["aios", "telegram", "--allow-user", "5"]), \
                mock.patch.object(tg, "run", return_value=0) as run, self.assertRaises(SystemExit):
            aios.cli()
        run.assert_called_once_with(".", -100123, {5})


if __name__ == "__main__":
    unittest.main()
