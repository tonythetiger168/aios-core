"""Agents as Discord bots: @mention your bot in a server channel and an AIOS agent replies.

    export AIOS_DISCORD_TOKEN=...   # Discord Developer Portal > your app > Bot > Reset Token
    python aios.py bot --platform discord --policy policy.toml --agent helper --allow-chat 1234567890 \\
        --provider openai --model gemma4:e4b

Invite the bot with the "bot" scope and the Send Messages + Read Message History permissions.
--allow-chat takes channel ids (Discord: Settings > Advanced > Developer Mode, then right-click a
channel > Copy Channel ID). A direct message to the bot is its own channel: the log prints its id.

It connects to the Discord gateway over a WebSocket (written here with the standard library) and
answers like aios_bot.BotCore says: when mentioned, on /ask, or on a reply to one of its answers.
Without --context it needs no privileged intent (Discord sends a bot the text of messages that
mention it and of DMs). With --context N turn on Message Content Intent in the portal (Bot page).

Answers are sent as replies with allowed_mentions empty, so nothing the model writes can ping
@everyone, a role or a person. At most 3 messages of 1900 characters. $HTTPS_PROXY is honoured.
"""
import asyncio
import base64
import hashlib
import json
import os
import random
import re
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import aios_bot

TOKEN_ENV = "AIOS_DISCORD_TOKEN"
API = "https://discord.com/api/v10"
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
UA = "DiscordBot (https://aios-registry-47b.pages.dev, 0.2)"
MSG_MAX = 1900  # Discord allows 2000 characters per message
FRAME_MAX = 8 << 20
GUILD_MESSAGES, DIRECT_MESSAGES, MESSAGE_CONTENT = 1 << 9, 1 << 12, 1 << 15
FATAL = {4004: "the token was refused", 4010: "invalid shard", 4011: "sharding required (too many servers)",
         4012: "invalid API version", 4013: "invalid intents",
         4014: "Message Content Intent is not enabled for this bot (Developer Portal > Bot), needed by --context"}
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class DiscordError(Exception):
    pass


class WSClosed(Exception):
    def __init__(self, code: int | None, reason: str = ""):
        super().__init__(f"websocket closed ({code} {reason})".strip())
        self.code = code


def http_api(token: str, base: str = API, timeout: float = 30):
    """method, path, body -> parsed JSON (or {}), with the bot token. Waits once on a 429."""
    def call(method: str, path: str, body: dict | None = None):
        for attempt in (0, 1):
            req = urllib.request.Request(f"{base}{path}", method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Authorization": f"Bot {token}", "User-Agent": UA,
                                                  "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                try:
                    info = json.loads(e.read() or b"{}")
                except ValueError:
                    info = {}
                if e.code == 429 and attempt == 0:
                    time.sleep(min(10.0, float(info.get("retry_after") or 1)))
                    continue
                raise DiscordError(f"{path}: HTTP {e.code} {info.get('message', '')}".rstrip()) from None
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise DiscordError(f"{path}: {type(e).__name__}: {getattr(e, 'reason', e)}") from None
        raise DiscordError(f"{path}: rate limited")
    return call


# ---- a small WebSocket client (RFC 6455), enough for the gateway ----
class WebSocket:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.lock = asyncio.Lock()

    @classmethod
    async def connect(cls, url: str, timeout: float = 30, proxy: str | None = None):
        u = urllib.parse.urlsplit(url)
        host, port = u.hostname, u.port or (443 if u.scheme == "wss" else 80)
        ctx = ssl.create_default_context() if u.scheme == "wss" else None
        if proxy:
            p = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
            reader, writer = await asyncio.wait_for(asyncio.open_connection(p.hostname, p.port or 80), timeout)
            auth = ""
            if p.username:
                cred = base64.b64encode(f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
                                        .encode()).decode()
                auth = f"Proxy-Authorization: Basic {cred}\r\n"
            writer.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{auth}\r\n".encode())
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
            status = head.split(b"\r\n", 1)[0].decode(errors="replace")
            if status.split()[1:2] != ["200"]:
                writer.close()
                raise DiscordError(f"proxy refused CONNECT: {status}")
            if ctx:
                await writer.start_tls(ctx, server_hostname=host)
        else:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        writer.write((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: {UA}\r\n\r\n").encode())
        head = (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)).decode(errors="replace")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        status = head.split("\r\n", 1)[0]
        if status.split()[1:2] != ["101"] or accept.lower() not in head.lower():
            writer.close()
            raise DiscordError(f"gateway refused the WebSocket upgrade: {status or '?'}")
        return cls(reader, writer)

    async def _frame(self, op: int, data: bytes):
        n = len(data)
        head = bytes([0x80 | op])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))  # clients must mask every frame
        async with self.lock:
            self.writer.write(head + mask + body)
            await self.writer.drain()

    async def send(self, text: str):
        await self._frame(0x1, text.encode())

    async def recv(self) -> str:
        parts, total = [], 0
        while True:
            b0, b1 = await self.reader.readexactly(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            mask = await self.reader.readexactly(4) if b1 & 0x80 else None
            total += n
            if total > FRAME_MAX:
                raise WSClosed(1009, "message too big")
            data = await self.reader.readexactly(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x8:
                code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else None
                raise WSClosed(code, data[2:].decode(errors="replace"))
            if op == 0x9:
                await self._frame(0xA, data)
                continue
            if op == 0xA:
                continue
            parts.append(data)
            if b0 & 0x80:
                return b"".join(parts).decode(errors="replace")

    async def close(self, code: int = 1000):
        try:
            await self._frame(0x8, struct.pack("!H", code))
        except (OSError, RuntimeError):
            pass
        self.writer.close()


# ---- the bot ----
class DiscordAdapter:
    """Discord REST + gateway events around a BotCore. api: http_api(...) or a fake (method, path, body)."""

    def __init__(self, api, core: aios_bot.BotCore, bot_id: str, parallel: int = 2):
        self.api, self.core, self.bot_id = api, core, str(bot_id)
        self.slots = asyncio.Semaphore(parallel)
        self.tasks: set = set()
        self.ping = re.compile(rf"<@!?{re.escape(self.bot_id)}>")

    async def call(self, method: str, path: str, body: dict | None = None):
        return await asyncio.to_thread(self.api, method, path, body)

    def parse(self, d: dict) -> dict:
        author = d.get("author") or {}
        text = d.get("content") or ""
        mentioned = any(str(x.get("id")) == self.bot_id for x in d.get("mentions") or []) or bool(self.ping.search(text))
        text = self.ping.sub("", text).strip() if mentioned else text
        r = d.get("referenced_message")
        reply = None
        if r:
            ra = r.get("author") or {}
            reply = {"id": str(r.get("id")), "from_bot": str(ra.get("id")) == self.bot_id, "name": name_of(ra),
                     "text": r.get("content") or ""}
            if reply["from_bot"]:
                mentioned = mentioned and bool(self.ping.search(d.get("content") or ""))  # reply-ping alone: BotCore decides
        return {"chat": str(d.get("channel_id")), "private": not d.get("guild_id"), "title": None,
                "id": str(d.get("id")), "user": str(author.get("id")), "name": name_of(author),
                "is_bot": bool(author.get("bot")), "thread": None, "text": text, "mentioned": mentioned,
                "reply_to": reply}

    async def respond(self, m: dict):
        async with self.slots:
            try:
                await self.call("POST", f"/channels/{m['chat']}/typing")
            except DiscordError:
                pass
            text = await self.core.handle(m)
            if text is None:
                return
            ids, to = [], m["id"]
            for part in aios_bot.split_reply(text, MSG_MAX):
                body = {"content": part, "allowed_mentions": {"parse": [], "replied_user": False},
                        "message_reference": {"message_id": to, "fail_if_not_exists": False}}
                try:
                    sent = await self.call("POST", f"/channels/{m['chat']}/messages", body)
                except DiscordError as e:
                    self.core.log(f"could not reply in channel {m['chat']}: {e}")
                    break
                ids.append(str(sent.get("id")))
                to = ids[-1]
            self.core.remember(m, text, ids)

    def message(self, d: dict):
        m = self.parse(d)
        if m["is_bot"] or m["user"] == self.bot_id:  # other bots and itself: no loops
            return
        if not m["text"] or not self.core.addressed(m):
            self.core.seen(m)
            return
        t = asyncio.create_task(self.respond(m))
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def drain(self):
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


def name_of(user: dict) -> str:
    n = re.sub(r"[\[\]<>\n\r]", "", user.get("global_name") or user.get("username") or "someone")[:40]
    return f"{n} (@{user['username']})" if user.get("username") and user.get("global_name") else n


async def session(ws, token: str, intents: int, on_message, on_ready=None):
    """One gateway session: identify, heartbeat, dispatch MESSAGE_CREATE until the socket closes."""
    hello = json.loads(await ws.recv())
    if hello.get("op") != 10:
        raise DiscordError("gateway did not say hello")
    every = hello["d"]["heartbeat_interval"] / 1000
    state = {"seq": None, "acked": True}

    async def beat():
        await asyncio.sleep(every * random.random())
        while True:
            if not state["acked"]:  # a zombie connection: drop it and connect again
                await ws.close(4000)
                return
            state["acked"] = False
            await ws.send(json.dumps({"op": 1, "d": state["seq"]}))
            await asyncio.sleep(every)

    await ws.send(json.dumps({"op": 2, "d": {"token": token, "intents": intents,
                                             "properties": {"os": sys.platform, "browser": "aios", "device": "aios"}}}))
    hb = asyncio.create_task(beat())
    try:
        while True:
            p = json.loads(await ws.recv())
            if p.get("s") is not None:
                state["seq"] = p["s"]
            op = p.get("op")
            if op == 11:
                state["acked"] = True
            elif op == 1:
                await ws.send(json.dumps({"op": 1, "d": state["seq"]}))
            elif op in (7, 9):  # reconnect / invalid session: start a fresh session
                return
            elif op == 0 and p.get("t") == "READY" and on_ready:
                on_ready(p["d"])
            elif op == 0 and p.get("t") == "MESSAGE_CREATE":
                on_message(p["d"])
    finally:
        hb.cancel()


async def run(answer, allow_chats: set | None, per_user: int = 20, context: int = 0, parallel: int = 2,
              api=None, describe: str = "", connect=None) -> int:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if api is None:
        if not token:
            print(f"set {TOKEN_ENV} to your bot's token (Discord Developer Portal > Bot)", file=sys.stderr)
            return 1
        api = http_api(token, os.environ.get("AIOS_DISCORD_API", API).rstrip("/"))
    try:
        me = await asyncio.to_thread(api, "GET", "/users/@me", None)
    except DiscordError as e:
        print(f"Discord refused the token or is unreachable: {e}", file=sys.stderr)
        return 1
    core = aios_bot.BotCore(answer, me.get("username", ""), allow_chats, per_user, context)
    bot = DiscordAdapter(api, core, me.get("id"), parallel)
    intents = GUILD_MESSAGES | DIRECT_MESSAGES | (MESSAGE_CONTENT if context else 0)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
    connect = connect or (lambda: WebSocket.connect(os.environ.get("AIOS_DISCORD_GATEWAY", GATEWAY), proxy=proxy))
    where = "every channel (--public)" if allow_chats is None else f"channels {sorted(map(str, allow_chats))}"
    print(f"[aios] bot: @{me.get('username', '?')} answers {describe} in {where}. Ctrl+C to stop.", file=sys.stderr)
    failures = 0
    try:
        while True:
            try:
                ws = await connect()
                try:
                    await session(ws, token, intents, bot.message)
                    failures = 0
                finally:
                    await ws.close()
            except WSClosed as e:
                if e.code in FATAL:
                    print(f"Discord closed the connection: {FATAL[e.code]}", file=sys.stderr)
                    return 1
                failures += 1
                core.log(f"{e} (reconnecting)")
            except (DiscordError, OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError) as e:
                failures += 1
                core.log(f"{type(e).__name__}: {e} (reconnecting)")
            await asyncio.sleep(min(60, 2 ** failures) if failures else 1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await bot.drain()
    return 0
