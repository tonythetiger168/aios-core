"""Answer approval requests from your phone: a Telegram bot for `--approvals console`.

    export AIOS_TELEGRAM_TOKEN=123456:ABC...        # from @BotFather; never put it on the command line
    python aios.py telegram --home ~/aios-data --chat-id 987654321

Every pending request in aios.db (from `daemon`, `serve` or `swarm` with `--approvals console`) is
sent to your chat with Approve / Deny buttons. A press is answered exactly like the web console does
it (aios_console.decide): only a request that is still pending and not expired changes. The message
is then edited to show the outcome, also when the request expired or was answered in the console.

Security model:
- Only button presses from the configured chat, by an allowed user (--allow-user; by default the
  chat id itself, which in a private chat is your user id), count. Everything else is ignored and
  logged to stderr. A bot's buttons cannot be pressed from another chat, but anyone who finds the
  bot can message it, so the allowlist is what matters.
- Messages are sent as plain text (no Markdown/HTML parsing), so text an attacker planted in tool
  arguments cannot hide or reformat parts of the request.
- What you approve leaves your machine: the agent, tool and arguments go through Telegram's servers.
  Do not use it for agents whose arguments are confidential. The bot token can answer for you:
  treat it like a password.
- The database is opened read-only except for decide()'s single UPDATE; the bot never creates it.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import aios_console

API = "https://api.telegram.org"  # or $AIOS_TELEGRAM_API (a self-hosted Bot API server)
TEXT_MAX = 3500  # Telegram allows 4096 chars per message; arguments are clipped to stay below
POLL = 2  # seconds one getUpdates long poll waits for a button press
TOKEN_ENV = "AIOS_TELEGRAM_TOKEN"


class TelegramError(RuntimeError):
    pass


def http_api(token: str, base: str = API, timeout: float = 30):
    """method, params -> result, using the Bot API over HTTPS (standard library only)."""
    def call(method: str, params: dict):
        req = urllib.request.Request(f"{base}/bot{token}/{method}", data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                body = json.load(e)
            except ValueError:
                raise TelegramError(f"{method}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:  # never print the URL: it holds the token
            raise TelegramError(f"{method}: {type(e).__name__}: {getattr(e, 'reason', e)}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', 'error')}")
        return body.get("result")
    return call


def request_text(aid, ts, agent, tool, args, tainted, expires) -> str:
    warn = ("\n⚠️ This run has read untrusted content (a file or tool result). Check that YOU want this, "
            "not text inside it.") if tainted else ""
    head = f"AIOS approval #{aid}\n{agent} wants to run {tool}{warn}\n\nArguments, as the model wrote them:\n"
    tail = f"\n\nAsked {ts} · closes {expires} (then refused)"
    room = TEXT_MAX - len(head) - len(tail)
    if len(args) > room:
        room -= 60  # for the note below
        args = args[:room] + f"... (+{len(args) - room} chars, see the console)"
    return head + args + tail


OUTCOME = {"approved": "✅ Approved", "denied": "❌ Denied", "expired": "⌛ Expired (refused)"}


class Bridge:
    """One loop step at a time (step()), so tests can drive it without threads or sleeping."""

    def __init__(self, home: str, chat_id: int, api, allow_users: set[int] | None = None, log=None):
        self.db = Path(home) / "aios.db"
        self.chat_id = int(chat_id)
        self.allow = allow_users or {self.chat_id}
        self.api = api
        self.log = log or (lambda msg: print(f"[aios] telegram: {msg}", file=sys.stderr))
        self.reader = aios_console.Reader(self.db)
        self.sent: dict[int, dict] = {}  # approval id -> {"message_id", "text"} of requests shown and open
        self.offset = 0

    def pending(self) -> list[tuple]:
        return self.reader.query("SELECT id, ts, agent, tool, args, tainted, expires FROM approvals "
                                 "WHERE status='pending' AND expires>? ORDER BY id", (aios_console.now_iso(),))

    def status(self, aid: int) -> str:
        rows = self.reader.query("SELECT status, expires FROM approvals WHERE id=?", (aid,))
        if not rows:
            return "expired"
        st, expires = rows[0]
        return "expired" if st == "pending" and expires <= aios_console.now_iso() else st

    def send_new(self):
        for row in self.pending():
            aid = row[0]
            if aid in self.sent:
                continue
            text = request_text(*row)
            msg = self.api("sendMessage", {
                "chat_id": self.chat_id, "text": text, "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"a:{aid}"},
                                                      {"text": "❌ Deny", "callback_data": f"d:{aid}"}]]}})
            self.sent[aid] = {"message_id": msg["message_id"], "text": text}

    def close_finished(self):
        """Requests answered elsewhere (console) or expired: show it and drop the buttons."""
        for aid in list(self.sent):
            st = self.status(aid)
            if st != "pending":
                self.finish(aid, OUTCOME.get(st, st))

    def finish(self, aid: int, outcome: str):
        info = self.sent.pop(aid, None)
        if info is None:
            return
        try:
            self.api("editMessageText", {"chat_id": self.chat_id, "message_id": info["message_id"],
                                         "text": f"{info['text']}\n\n{outcome}"})
        except TelegramError as e:  # e.g. the user deleted the message: nothing left to update
            self.log(f"could not update the message for #{aid}: {e}")

    def handle(self, update: dict):
        q = update.get("callback_query")
        if not q:
            msg = update.get("message") or {}
            if (msg.get("chat") or {}).get("id") == self.chat_id and msg.get("text", "").startswith("/start"):
                self.api("sendMessage", {"chat_id": self.chat_id,
                                         "text": "AIOS approvals are on: requests will appear here."})
            return
        user = (q.get("from") or {}).get("id")
        chat = ((q.get("message") or {}).get("chat") or {}).get("id")
        if chat != self.chat_id or user not in self.allow:
            self.log(f"ignored a button press from user {user} in chat {chat} (not allowed)")
            self.api("answerCallbackQuery", {"callback_query_id": q["id"], "text": "Not allowed."})
            return
        kind, _, num = str(q.get("data", "")).partition(":")
        if kind not in ("a", "d") or not num.isdigit():
            self.api("answerCallbackQuery", {"callback_query_id": q["id"]})
            return
        aid, approve = int(num), kind == "a"
        ok = aios_console.decide(self.db, aid, approve)
        st = ("approved" if approve else "denied") if ok else self.status(aid)
        outcome = OUTCOME.get(st, st)
        if not ok:
            outcome = f"Already answered or expired: {outcome}"
        self.log(f"#{aid} {('approved' if approve else 'denied') if ok else 'was already closed'} by user {user}")
        self.api("answerCallbackQuery", {"callback_query_id": q["id"], "text": outcome})
        m = q.get("message") or {}
        if aid not in self.sent and "message_id" in m:  # a button on a message from an earlier run of the bot
            self.sent[aid] = {"message_id": m["message_id"], "text": m.get("text", "")}
        self.finish(aid, outcome)

    def step(self, poll: float = POLL):
        self.send_new()
        updates = self.api("getUpdates", {"offset": self.offset, "timeout": int(poll),
                                          "allowed_updates": ["callback_query", "message"]})
        for u in updates or []:
            self.offset = max(self.offset, u["update_id"] + 1)
            self.handle(u)
        self.close_finished()


def run(home: str, chat_id: int, allow_users: set[int] | None = None, api=None) -> int:
    if not (Path(home) / "aios.db").exists():
        print(f"no aios.db in {Path(home).resolve()}: start the daemon (or serve) with this --home first",
              file=sys.stderr)
        return 1
    if api is None:
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token:
            print(f"set {TOKEN_ENV} to your bot's token (from @BotFather)", file=sys.stderr)
            return 1
        api = http_api(token, os.environ.get("AIOS_TELEGRAM_API", API).rstrip("/"), timeout=POLL + 30)
    try:
        me = api("getMe", {})
    except TelegramError as e:
        print(f"Telegram refused the token or is unreachable: {e}", file=sys.stderr)
        return 1
    bridge = Bridge(home, chat_id, api, allow_users)
    print(f"[aios] telegram: @{me.get('username', '?')} sends approval requests for {Path(home).resolve()} "
          f"to chat {chat_id}. Ctrl+C to stop.", file=sys.stderr)
    failures = 0
    try:
        while True:
            try:
                bridge.step()
                failures = 0
            except (TelegramError, sqlite3.Error) as e:  # network blips: back off, keep going
                failures += 1
                bridge.log(f"{e} (retrying)")
                time.sleep(min(60, 2 ** failures))
    except KeyboardInterrupt:
        pass
    return 0
