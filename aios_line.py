"""Agents as LINE bots: @mention your LINE Official Account in a group and an AIOS agent replies.

    export AIOS_LINE_TOKEN=...    # channel access token (LINE Developers > Messaging API)
    export AIOS_LINE_SECRET=...   # channel secret (Basic settings): webhook calls are checked with it
    python aios.py bot --platform line --policy policy.toml --agent helper --allow-chat C0123...  \\
        --provider openai --model gemma4:e4b
    cloudflared tunnel --url http://127.0.0.1:8790    # LINE needs an HTTPS webhook URL: use any tunnel
    # LINE Developers > Messaging API > Webhook URL = https://<tunnel>/line  (turn "Use webhook" on)

LINE pushes events to a webhook (there is no polling), so this listens on 127.0.0.1:--port and you
expose it with a tunnel. Every call must carry a valid X-Line-Signature (HMAC-SHA256 of the body
with the channel secret); others get 401 and are logged. The rest is BotCore (aios_bot): the same
rules for when to answer, the same quoting, the same tainted runs, per-user and daily limits.

- Chat ids: groups start with C, multi-person chats with R, one-to-one chats with U (the person's
  user id). The log prints the id of chats that are not allowed yet, so you can copy it.
- Answers use the free reply API and quote the question. A reply token only lives about a minute:
  if the answer takes longer it is sent with the push API instead, which counts against the
  account's monthly message quota (--no-push: drop slow answers instead).
- Answers are plain text, at most 3 messages of 4900 characters.
Standard library only.
"""
import asyncio
import base64
import collections
import hashlib
import hmac
import http.server
import json
import os
import sys
import threading
import urllib.error
import urllib.request

import aios_bot

TOKEN_ENV, SECRET_ENV = "AIOS_LINE_TOKEN", "AIOS_LINE_SECRET"
API = "https://api.line.me"
PATH = "/line"
MSG_MAX = 4900  # LINE allows 5000 characters per text message
BODY_MAX = 1 << 20
SEEN_KEPT = 2000  # message ids remembered so a quoted message can be shown to the agent


class LineError(Exception):
    pass


def http_api(token: str, base: str = API, timeout: float = 30):
    """method, path, body -> parsed JSON (or {}), over HTTPS with the channel access token."""
    def call(method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(f"{base}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read() or b"{}").get("message", "")
            except ValueError:
                msg = ""
            raise LineError(f"{path}: HTTP {e.code} {msg}".rstrip()) from None
        except (urllib.error.URLError, OSError) as e:
            raise LineError(f"{path}: {type(e).__name__}: {getattr(e, 'reason', e)}") from None
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {}
    return call


def signature_ok(secret: str, body: bytes, sig: str | None) -> bool:
    if not sig or not secret:
        return False
    want = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(want, sig.strip())


class LineAdapter:
    """LINE Messaging API around a BotCore. api: http_api(...) or a fake taking (method, path, body)."""

    def __init__(self, api, core: aios_bot.BotCore, bot_id: str, parallel: int = 2, push: bool = True):
        self.api, self.core, self.bot_id, self.push = api, core, bot_id, push
        self.slots = asyncio.Semaphore(parallel)
        self.tasks: set = set()
        self.seen: collections.OrderedDict = collections.OrderedDict()  # message id -> {"name", "text", "from_bot"}
        self.names: dict = {}

    async def call(self, method: str, path: str, body: dict | None = None):
        return await asyncio.to_thread(self.api, method, path, body)

    def keep(self, mid, name: str, text: str, from_bot: bool = False):
        if mid:
            self.seen[str(mid)] = {"name": name, "text": text, "from_bot": from_bot}
            while len(self.seen) > SEEN_KEPT:
                self.seen.popitem(last=False)

    async def name(self, src: dict) -> str:
        uid = src.get("userId")
        if not uid:
            return "someone"
        if uid not in self.names:
            path = f"/v2/bot/profile/{uid}"
            if src.get("type") == "group":
                path = f"/v2/bot/group/{src['groupId']}/member/{uid}"
            elif src.get("type") == "room":
                path = f"/v2/bot/room/{src['roomId']}/member/{uid}"
            try:
                p = await self.call("GET", path)
                self.names[uid] = aios_bot.name_of({"first_name": p.get("displayName") or "someone"})
            except LineError:
                self.names[uid] = "someone"  # not a friend of the account, or no permission: stay anonymous
        return self.names[uid]

    async def parse(self, ev: dict) -> dict | None:
        msg, src = ev.get("message") or {}, ev.get("source") or {}
        if ev.get("type") != "message" or msg.get("type") != "text":
            return None
        kind = src.get("type")
        chat = src.get("groupId") or src.get("roomId") or src.get("userId")
        text, mentioned = msg.get("text") or "", False
        cut = []
        for x in (msg.get("mention") or {}).get("mentionees") or []:
            if x.get("isSelf") or (self.bot_id and x.get("userId") == self.bot_id):
                mentioned = True
                cut.append((x.get("index", 0), x.get("length", 0)))
        for i, n in sorted(cut, reverse=True):  # drop "@Bot name" (names can hold spaces, so by position)
            text = text[:i] + text[i + n:]
        reply = None
        q = msg.get("quotedMessageId")
        if q:
            old = self.seen.get(str(q)) or {}
            reply = {"id": str(q), "from_bot": bool(old.get("from_bot")), "name": old.get("name", "someone"),
                     "text": old.get("text", "")}
        name = await self.name(src)
        self.keep(msg.get("id"), name, msg.get("text") or "")
        return {"chat": chat, "private": kind == "user", "title": None, "id": str(msg.get("id")),
                "user": src.get("userId") or chat, "name": name, "is_bot": False, "thread": None,
                "text": text.strip() if mentioned else text, "mentioned": mentioned, "reply_to": reply,
                "reply_token": ev.get("replyToken"), "quote_token": msg.get("quoteToken")}

    async def respond(self, m: dict):
        async with self.slots:
            if m["private"]:  # the "..." animation exists for one-to-one chats only
                try:
                    await self.call("POST", "/v2/bot/chat/loading/start", {"chatId": m["chat"], "loadingSeconds": 60})
                except LineError:
                    pass
            text = await self.core.handle(m)
            if text is None:
                return
            parts = aios_bot.split_reply(text, MSG_MAX)
            msgs = [{"type": "text", "text": p} for p in parts]
            if m.get("quote_token"):
                msgs[0]["quoteToken"] = m["quote_token"]
            sent = None
            try:
                sent = await self.call("POST", "/v2/bot/message/reply", {"replyToken": m["reply_token"], "messages": msgs})
            except LineError as e:
                if not self.push:
                    self.core.log(f"could not reply in chat {m['chat']} ({e}); --no-push, so the answer is dropped")
                    return
                self.core.log(f"reply token expired or refused in chat {m['chat']} ({e}); sending with the push API")
                try:
                    sent = await self.call("POST", "/v2/bot/message/push", {"to": m["chat"], "messages": msgs})
                except LineError as e2:
                    self.core.log(f"could not push to chat {m['chat']}: {e2}")
                    return
            ids = [str(x.get("id")) for x in (sent or {}).get("sentMessages") or [] if x.get("id")]
            for i, part in zip(ids, parts):
                self.keep(i, "you", part, from_bot=True)
            self.core.remember(m, text, ids)

    async def events(self, payload: dict):
        for ev in payload.get("events") or []:
            m = await self.parse(ev)
            if m is None or m["chat"] is None:
                continue
            if not m["text"] or not self.core.addressed(m):
                self.core.seen(m)
                continue
            if not self.core.allowed(m):
                self.core.log(f"ignored a message in chat {m['chat']} (not in --allow-chat)")
                continue
            t = asyncio.create_task(self.respond(m))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)

    async def drain(self):
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


def make_server(port: int, secret: str, deliver, log, host: str = "127.0.0.1"):
    """A webhook server: deliver(payload) is called (from the server thread) for each signed POST."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code: int):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > BODY_MAX:
                self.close_connection = True
                return self.reply(413)
            body = self.rfile.read(n)  # read before refusing: some clients reset otherwise
            if self.path.split("?")[0] != PATH:
                return self.reply(404)
            if not signature_ok(secret, body, self.headers.get("X-Line-Signature")):
                log("refused a webhook call with a missing or wrong X-Line-Signature")
                return self.reply(401)
            try:
                payload = json.loads(body)
            except ValueError:
                return self.reply(400)
            self.reply(200)  # answer LINE at once; the agent runs after
            deliver(payload)

        def do_GET(self):
            self.reply(404)

    srv = http.server.ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    return srv


async def run(answer, allow_chats: set | None, per_user: int = 20, context: int = 0, parallel: int = 2,
              api=None, describe: str = "", port: int = 8790, push: bool = True, secret: str | None = None) -> int:
    secret = secret if secret is not None else os.environ.get(SECRET_ENV, "").strip()
    if api is None:
        token = os.environ.get(TOKEN_ENV, "").strip()
        if not token or not secret:
            print(f"set {TOKEN_ENV} (channel access token) and {SECRET_ENV} (channel secret) from LINE Developers",
                  file=sys.stderr)
            return 1
        api = http_api(token, os.environ.get("AIOS_LINE_API", API).rstrip("/"))
    try:
        me = await asyncio.to_thread(api, "GET", "/v2/bot/info", None)
    except LineError as e:
        print(f"LINE refused the token or is unreachable: {e}", file=sys.stderr)
        return 1
    core = aios_bot.BotCore(answer, "", allow_chats, per_user, context)
    bot = LineAdapter(api, core, me.get("userId", ""), parallel, push)
    loop = asyncio.get_running_loop()

    def deliver(payload):
        asyncio.run_coroutine_threadsafe(bot.events(payload), loop)

    try:
        srv = make_server(port, secret, deliver, core.log)
    except OSError as e:
        print(f"cannot listen on 127.0.0.1:{port}: {e}", file=sys.stderr)
        return 1
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    where = "every chat (--public)" if allow_chats is None else f"chats {sorted(map(str, allow_chats))}"
    print(f"[aios] bot: LINE account {me.get('displayName', '?')} answers {describe} in {where}.\n"
          f"[aios] bot: webhook on http://127.0.0.1:{port}{PATH}: expose it with a tunnel and set it as the "
          f"Webhook URL in LINE Developers. Ctrl+C to stop.", file=sys.stderr)
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        srv.shutdown()
        srv.server_close()
        await bot.drain()
    return 0
