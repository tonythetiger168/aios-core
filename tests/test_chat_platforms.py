"""Chat bots on Discord (aios_discord) and LINE (aios_line): same BotCore rules, platform plumbing."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_bot  # noqa: E402
import aios_discord  # noqa: E402
import aios_line  # noqa: E402

BOT = "900"
CHAN, OTHER = "111", "222"


class Answers:
    def __init__(self, reply="ok"):
        self.prompts, self.reply = [], reply

    async def __call__(self, task, who):
        self.prompts.append((task, who))
        return self.reply(task) if callable(self.reply) else self.reply


# ---- Discord ----
class FakeDiscord:
    def __init__(self):
        self.calls, self.next_id = [], 5000

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/users/@me":
            return {"id": BOT, "username": "aios"}
        if path.endswith("/messages"):
            self.next_id += 1
            return {"id": str(self.next_id)}
        return {}

    def sent(self):
        return [(p, b) for m, p, b in self.calls if p.endswith("/messages")]


def dmsg(content, mid="1", chan=CHAN, user="7", name="alice", bot=False, mentions=(), guild="g1", ref=None):
    d = {"id": mid, "channel_id": chan, "content": content, "author": {"id": user, "username": name, "bot": bot},
         "mentions": [{"id": x} for x in mentions]}
    if guild:
        d["guild_id"] = guild
    if ref:
        d["referenced_message"] = ref
    return d


class DiscordTests(unittest.IsolatedAsyncioTestCase):
    def make(self, allow=frozenset({CHAN}), reply="ok"):
        self.api, self.ans, self.logs = FakeDiscord(), Answers(reply), []
        core = aios_bot.BotCore(self.ans, "aios", None if allow is None else set(allow), log=self.logs.append)
        self.bot = aios_discord.DiscordAdapter(self.api, core, BOT)
        return self.bot

    async def feed(self, *msgs):
        for d in msgs:
            self.bot.message(d)
        await self.bot.drain()

    async def test_answers_mentions_and_ask_only(self):
        self.make()
        await self.feed(dmsg("lunch?"), dmsg(f"<@{BOT}> what is 2+2?", mid="2", mentions=[BOT]),
                        dmsg("/ask capital of France", mid="3"), dmsg(f"<@!{BOT}> nick form", mid="4"))
        self.assertEqual(len(self.ans.prompts), 3)
        self.assertTrue(all(f"<@{BOT}>" not in t.split("Their message")[1] for t, _ in self.ans.prompts))
        sent = self.api.sent()
        self.assertEqual(sorted(b["message_reference"]["message_id"] for _, b in sent), ["2", "3", "4"])
        for _, b in sent:  # the model can never ping anyone
            self.assertEqual(b["allowed_mentions"], {"parse": [], "replied_user": False})

    async def test_reply_to_answer_continues_thread_and_quotes_posts(self):
        self.make(reply=lambda t: "Paris" if "France" in t.split("Their message")[1] else "2.1 million")
        await self.feed(dmsg(f"<@{BOT}> capital of France?", mid="10", mentions=[BOT]))
        answer_id = str(self.api.next_id)
        ref = {"id": answer_id, "content": "Paris", "author": {"id": BOT, "username": "aios", "bot": True}}
        await self.feed(dmsg("and its population?", mid="11", ref=ref, mentions=[BOT]))
        task = self.ans.prompts[1][0]
        self.assertIn("continues an earlier exchange", task)
        self.assertIn("[you]: Paris", task)
        post = {"id": "20", "content": "The moon is cheese.", "author": {"id": "8", "username": "bob"}}
        await self.feed(dmsg(f"<@{BOT}> true?", mid="21", ref=post, mentions=[BOT]))
        self.assertIn("The moon is cheese.", self.ans.prompts[2][0])
        self.assertIn("by bob", self.ans.prompts[2][0])

    async def test_other_channels_bots_and_itself_are_ignored(self):
        self.make()
        await self.feed(dmsg(f"<@{BOT}> hi", chan=OTHER, mentions=[BOT]),
                        dmsg(f"<@{BOT}> loop", bot=True, user="66", mentions=[BOT]),
                        dmsg("/ask me", user=BOT))
        self.assertEqual(self.ans.prompts, [])
        self.assertTrue(any("not in --allow-chat" in x for x in self.logs))

    async def test_direct_message_needs_no_mention(self):
        self.make(allow={"333"})
        await self.feed(dmsg("hello", chan="333", guild=None))
        self.assertEqual(len(self.ans.prompts), 1)
        self.assertIn("a private chat", self.ans.prompts[0][0])

    async def test_long_answer_is_split_and_chained(self):
        self.make(reply="word " * 1000)
        await self.feed(dmsg("/ask essay", mid="30"))
        sent = self.api.sent()
        self.assertEqual(len(sent), 3)
        self.assertTrue(all(len(b["content"]) <= 2000 for _, b in sent))
        self.assertEqual(sent[1][1]["message_reference"]["message_id"], str(self.api.next_id - 2))


class FakeGateway:
    """A local WebSocket server speaking just enough of the Discord gateway."""

    def __init__(self, events, close_code=4004):
        self.events, self.close_code, self.got = events, close_code, []

    async def handle(self, reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        key = next(x.split(":", 1)[1].strip() for x in head.split("\r\n") if x.lower().startswith("sec-websocket-key"))
        accept = base64.b64encode(hashlib.sha1((key + aios_discord.WS_GUID).encode()).digest()).decode()
        writer.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        ws = aios_discord.WebSocket(reader, writer)
        server_send = self.server_frame(writer)
        await server_send(json.dumps({"op": 10, "d": {"heartbeat_interval": 45000}}))
        self.got.append(json.loads(await ws.recv()))  # identify (a client frame, masked)
        for i, ev in enumerate(self.events, 1):
            await server_send(json.dumps({"op": 0, "s": i, "t": ev[0], "d": ev[1]}))
        await asyncio.sleep(0.2)
        writer.write(bytes([0x88, 2]) + self.close_code.to_bytes(2, "big"))
        await writer.drain()
        writer.close()

    @staticmethod
    def server_frame(writer):
        async def send(text):
            data = text.encode()
            n = len(data)
            head = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + n.to_bytes(2, "big"))
            writer.write(head + data)  # servers do not mask
            await writer.drain()
        return send


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_connects_identifies_answers_and_stops_on_fatal_close(self):
        gw = FakeGateway([("READY", {"user": {"id": BOT}}),
                          ("MESSAGE_CREATE", dmsg(f"<@{BOT}> ping?", mid="77", mentions=[BOT]))])
        srv = await asyncio.start_server(gw.handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        api, ans = FakeDiscord(), Answers("pong")
        try:
            with mock.patch.dict(os.environ, {aios_discord.TOKEN_ENV: "tok"}):
                code = await asyncio.wait_for(aios_discord.run(
                    ans, {CHAN}, api=api, connect=lambda: aios_discord.WebSocket.connect(f"ws://127.0.0.1:{port}/")), 20)
        finally:
            srv.close()
        self.assertEqual(code, 1)  # 4004 = token refused: no endless reconnects
        ident = gw.got[0]
        self.assertEqual(ident["op"], 2)
        self.assertEqual(ident["d"]["token"], "tok")
        self.assertFalse(ident["d"]["intents"] & aios_discord.MESSAGE_CONTENT)  # not privileged without --context
        self.assertEqual(api.sent()[0][1]["content"], "pong")


# ---- LINE ----
class FakeLine:
    def __init__(self, reply_fails=False):
        self.calls, self.next_id, self.reply_fails = [], 7000, reply_fails

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/v2/bot/info":
            return {"userId": "Ubot", "displayName": "AIOS"}
        if "/member/" in path or path.startswith("/v2/bot/profile/"):
            return {"displayName": "Alice"}
        if path == "/v2/bot/message/reply" and self.reply_fails:
            raise aios_line.LineError("/v2/bot/message/reply: HTTP 400 Invalid reply token")
        if path in ("/v2/bot/message/reply", "/v2/bot/message/push"):
            out = []
            for _ in body["messages"]:
                self.next_id += 1
                out.append({"id": str(self.next_id), "quoteToken": "q"})
            return {"sentMessages": out}
        return {}

    def sent(self, path="/v2/bot/message/reply"):
        return [b for m, p, b in self.calls if p == path]


def lev(text, mid="1", group="Cgroup", user="Ualice", mention=None, quoted=None, private=False):
    msg = {"type": "text", "id": mid, "text": text, "quoteToken": f"qt{mid}"}
    if mention:
        msg["mention"] = {"mentionees": mention}
    if quoted:
        msg["quotedMessageId"] = quoted
    src = {"type": "user", "userId": user} if private else {"type": "group", "groupId": group, "userId": user}
    return {"type": "message", "replyToken": f"rt{mid}", "source": src, "message": msg}


class LineTests(unittest.IsolatedAsyncioTestCase):
    def make(self, allow=frozenset({"Cgroup"}), reply="ok", **kw):
        self.api, self.ans, self.logs = FakeLine(**kw), Answers(reply), []
        core = aios_bot.BotCore(self.ans, "", None if allow is None else set(allow), log=self.logs.append)
        self.bot = aios_line.LineAdapter(self.api, core, "Ubot", push=True)
        return self.bot

    async def feed(self, *evs):
        await self.bot.events({"events": list(evs)})
        await self.bot.drain()

    async def test_mention_is_answered_with_a_quoted_reply(self):
        self.make()
        await self.feed(lev("lunch?"),
                        lev("@AIOS Bot what is 2+2?", mid="2", mention=[{"index": 0, "length": 9, "isSelf": True}]),
                        lev("/ask hi", mid="3"))
        self.assertEqual(len(self.ans.prompts), 2)
        self.assertNotIn("@AIOS Bot", self.ans.prompts[0][0].split("Their message")[1])
        self.assertIn("from Alice", self.ans.prompts[0][0])
        sent = self.api.sent()
        self.assertEqual(sorted(b["replyToken"] for b in sent), ["rt2", "rt3"])
        self.assertTrue(all(b["messages"][0]["quoteToken"].startswith("qt") for b in sent))

    async def test_quoted_post_and_reply_to_answer(self):
        self.make(reply=lambda t: "Paris" if "France" in t.split("Their message")[1] else "about 2 million")
        await self.feed(lev("The moon is cheese.", mid="5", user="Ubob"))
        await self.feed(lev("/ask is this true?", mid="6", quoted="5"))
        self.assertIn("The moon is cheese.", self.ans.prompts[0][0])
        await self.feed(lev("/ask capital of France?", mid="7"))
        answer_id = str(self.api.next_id)
        await self.feed(lev("and its population?", mid="8", quoted=answer_id))  # no mention: a reply to the bot
        task = self.ans.prompts[2][0]
        self.assertIn("continues an earlier exchange", task)
        self.assertIn("[you]: Paris", task)

    async def test_other_chats_ignored_private_needs_no_mention(self):
        self.make(allow={"Ualice"})
        await self.feed(lev("/ask hi", group="Cother"), lev("hello", mid="9", private=True))
        self.assertEqual(len(self.ans.prompts), 1)
        self.assertTrue(any("Cother" in x and "not in --allow-chat" in x for x in self.logs))
        self.assertTrue(any(p == "/v2/bot/chat/loading/start" for _, p, _ in self.api.calls))

    async def test_expired_reply_token_falls_back_to_push_or_drops(self):
        self.make(reply_fails=True)
        await self.feed(lev("/ask slow question", mid="10"))
        self.assertEqual(self.api.sent("/v2/bot/message/push")[0]["to"], "Cgroup")
        api, core = FakeLine(reply_fails=True), aios_bot.BotCore(Answers(), "", {"Cgroup"}, log=lambda m: None)
        bot = aios_line.LineAdapter(api, core, "Ubot", push=False)
        await bot.events({"events": [lev("/ask x", mid="11")]})
        await bot.drain()
        self.assertEqual(api.sent("/v2/bot/message/push"), [])

    def test_signature(self):
        body = b'{"events":[]}'
        sig = base64.b64encode(hmac.new(b"sec", body, hashlib.sha256).digest()).decode()
        self.assertTrue(aios_line.signature_ok("sec", body, sig))
        self.assertFalse(aios_line.signature_ok("sec", body + b" ", sig))
        self.assertFalse(aios_line.signature_ok("sec", body, None))
        self.assertFalse(aios_line.signature_ok("", body, sig))


class WebhookTests(unittest.TestCase):
    def test_server_checks_signature_before_delivering(self):
        got, logs = [], []
        srv = aios_line.make_server(0, "sec", got.append, logs.append)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            def post(body, sig=None, path="/line"):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST",
                                             headers={"X-Line-Signature": sig} if sig else {})
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    return e.code
            body = json.dumps({"events": [lev("hi")]}).encode()
            good = base64.b64encode(hmac.new(b"sec", body, hashlib.sha256).digest()).decode()
            self.assertEqual(post(body), 401)
            self.assertEqual(post(body, "AAAA"), 401)
            self.assertEqual(post(body, good, "/other"), 404)
            self.assertEqual(got, [])
            self.assertEqual(post(body, good), 200)
            self.assertEqual(got[0]["events"][0]["message"]["text"], "hi")
            self.assertTrue(any("X-Line-Signature" in x for x in logs))
        finally:
            srv.shutdown()
            srv.server_close()


class CliTests(unittest.TestCase):
    def test_platform_flags(self):
        calls = []

        async def fake_bot_cmd(*args, **kw):
            calls.append((args, kw))
            return 0

        with tempfile.TemporaryDirectory() as home, mock.patch.object(aios, "bot_cmd", fake_bot_cmd), \
                mock.patch.object(sys, "argv", ["aios", "bot", "--platform", "line", "--allow-chat", "Cabc",
                                                "--home", home, "--provider", "openai", "--model", "m", "--no-push", "--port", "9000"]):
            with self.assertRaises(SystemExit) as e:
                aios.cli()
        self.assertEqual(e.exception.code, 0)
        args, kw = calls[0]
        self.assertEqual(args[5], {"Cabc"})
        self.assertEqual((kw["platform"], kw["port"], kw["push"]), ("line", 9000, False))

    def test_telegram_ids_must_be_numbers(self):
        with mock.patch.object(sys, "argv", ["aios", "bot", "--allow-chat", "Cabc", "--provider", "openai", "--model", "m"]), \
                mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as e:
                aios.cli()
        self.assertEqual(e.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
