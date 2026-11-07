"""Chat bots (aios_bot): agents that answer @mentions in Telegram chats, in the thread."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_bot  # noqa: E402

BOT_ID, GROUP, ALICE, BOB = 42, -100777, 1, 2


def text(t):
    return aios.Completion([{"type": "text", "text": t}], "end_turn")


def call(name, args):
    return aios.Completion([{"type": "tool_use", "id": "c1", "name": name, "input": args}], "tool_use")


class Scripted:
    def __init__(self, *script):
        self.script, self.calls = list(script), []

    async def complete(self, system, messages, tools, max_tokens=1024):
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "tools": [t["name"] for t in tools]})
        return self.script.pop(0) if self.script else text("done")


class FakeAPI:
    def __init__(self):
        self.calls, self.updates, self.next_id = [], [], 500

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        if method == "getMe":
            return {"id": BOT_ID, "username": "aios_bot"}
        return True

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]

    def post(self, txt, user=ALICE, chat=GROUP, mid=None, reply_to=None, is_bot=False, private=False, first="Alice"):
        mid = mid or 10 + len(self.calls) + len(self.updates)
        msg = {"message_id": mid, "text": txt, "from": {"id": user, "first_name": first, "is_bot": is_bot},
               "chat": {"id": chat, "type": "private" if private else "supergroup", "title": "Friends"}}
        if reply_to:
            msg["reply_to_message"] = reply_to
        self.updates.append({"update_id": len(self.calls) + len(self.updates) + 1, "message": msg})
        return msg


class Answers:
    def __init__(self, reply="ok"):
        self.prompts, self.reply = [], reply

    async def __call__(self, task, who):
        self.prompts.append((task, who))
        return self.reply(task) if callable(self.reply) else self.reply


class CoreTests(unittest.IsolatedAsyncioTestCase):
    def make(self, allow=frozenset({GROUP}), **kw):
        self.api, self.ans, self.logs = FakeAPI(), Answers(kw.pop("reply", "ok")), []
        core = aios_bot.BotCore(self.ans, "aios_bot", None if allow is None else set(allow), log=self.logs.append, **kw)
        self.bot = aios_bot.TelegramAdapter(self.api, core, BOT_ID)
        return self.bot

    async def step(self):
        await self.bot.step()
        await self.bot.drain()

    async def test_answers_only_when_addressed_in_a_group(self):
        self.make()
        self.api.post("lunch at noon?")
        self.api.post("@aios_bot what is 2+2?", mid=20)
        self.api.post("/ask capital of France", mid=21)
        self.api.post("/ask@aios_bot hi", mid=22)
        self.api.post("email me at x@aios_botnet.com", mid=23)  # not a mention: a longer name
        await self.step()
        self.assertEqual(len(self.ans.prompts), 3)
        self.assertIn("what is 2+2?", self.ans.prompts[0][0])
        self.assertNotIn("@aios_bot", self.ans.prompts[0][0].split("Their message")[1])
        replies = self.api.sent()
        self.assertEqual([r["reply_parameters"]["message_id"] for r in replies], [20, 21, 22])
        self.assertTrue(all("parse_mode" not in r for r in replies))

    async def test_tag_under_a_post_quotes_that_post(self):
        self.make()
        post = self.api.post("The moon is made of cheese.", user=BOB, first="Bob", mid=30)
        await self.step()
        self.api.post("@aios_bot is this true?", reply_to=post, mid=31)
        await self.step()
        task = self.ans.prompts[0][0]
        self.assertIn("They replied to this message, by Bob", task)
        self.assertIn("The moon is made of cheese.", task)
        self.assertIn("written by chat members, not by your user", task)

    async def test_follow_up_reply_keeps_the_thread(self):
        self.make(reply=lambda t: "Paris" if "France" in t.split("Their message")[1] else "about 2.1 million")
        self.api.post("@aios_bot capital of France?", mid=40)
        await self.step()
        answer_id = self.api.next_id  # the id the fake gave the answer
        bot_msg = {"message_id": answer_id, "text": "Paris", "from": {"id": BOT_ID, "is_bot": True, "first_name": "AIOS"}}
        self.api.post("and its population?", reply_to=bot_msg, mid=41, user=BOB, first="Bob")  # no mention needed
        await self.step()
        task = self.ans.prompts[1][0]
        self.assertIn("continues an earlier exchange", task)
        self.assertIn("capital of France?", task)
        self.assertIn("[you]: Paris", task)
        self.assertNotIn("They replied to this message", task)  # the bot's own answer is not a "post"

    async def test_other_chats_and_bots_are_ignored(self):
        self.make()
        self.api.post("@aios_bot hi", chat=-100999)
        self.api.post("@aios_bot loop?", is_bot=True, user=7)
        await self.step()
        self.assertEqual(self.ans.prompts, [])
        self.assertEqual(self.api.sent(), [])
        self.assertTrue(any("not in --allow-chat" in x for x in self.logs))

    async def test_public_and_private_chats(self):
        self.make(allow=None)
        self.api.post("hello there", chat=ALICE, private=True)  # private: no mention needed
        self.api.post("@aios_bot hi", chat=-100999)
        await self.step()
        self.assertEqual(len(self.ans.prompts), 2)
        self.assertIn("a private chat", self.ans.prompts[0][0])

    async def test_per_user_limit(self):
        self.make(per_user=2)
        for i in range(3):
            self.api.post(f"@aios_bot q{i}", mid=60 + i)
        self.api.post("@aios_bot from bob", user=BOB, mid=70)
        await self.step()
        self.assertEqual(len(self.ans.prompts), 3)
        self.assertIn("2 questions this hour", self.api.sent()[2]["text"])

    async def test_context_window(self):
        self.make(context=2)
        self.api.post("first", mid=80)
        self.api.post("second", mid=81, user=BOB, first="Bob")
        self.api.post("third", mid=82)
        self.api.post("@aios_bot summarise", mid=83)
        await self.step()
        task = self.ans.prompts[0][0]
        self.assertIn("[Bob]: second", task)
        self.assertIn("[Alice]: third", task)
        self.assertNotIn("]: first", task)

    async def test_failures_become_short_notes(self):
        async def boom(task, who):
            raise aios.DailyLimitReached("daily token budget reached")
        self.make()
        self.bot.core.answer = boom
        self.api.post("@aios_bot hi")
        await self.step()
        self.assertIn("today's budget", self.api.sent()[0]["text"])

    def test_split_reply(self):
        parts = aios_bot.split_reply("word " * 3000, size=1000, parts=3)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertTrue(parts[-1].endswith("(answer cut)"))
        self.assertEqual(aios_bot.split_reply("short"), ["short"])
        self.assertEqual(aios_bot.split_reply("  "), ["(no answer)"])

    async def test_long_answer_is_threaded_replies(self):
        self.make(reply="x " * 5000)
        self.api.post("@aios_bot long", mid=90)
        await self.step()
        sent = self.api.sent()
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[1]["reply_parameters"]["message_id"], 501)  # each part replies to the last


class AgentTests(unittest.IsolatedAsyncioTestCase):
    """make_bot_answerer: a real kernel run per question."""

    async def asyncSetUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = d.name
        old = aios.WORKSPACE
        self.addCleanup(setattr, aios, "WORKSPACE", old)
        aios.WORKSPACE = Path(d.name) / "workspace"
        aios.WORKSPACE.mkdir()

    def kernel(self, *script):
        k = aios.Kernel(store=aios.Store(os.path.join(self.home, "aios.db")), provider=Scripted(*script), approver=None)
        aios.install_builtin_tools(k)
        self.addCleanup(k.store.db.close)
        return k

    async def test_read_only_and_risky_calls_refused(self):
        k = self.kernel(call("write_file", {"path": "x.txt", "content": "pwned"}), text("I can't do that."))
        policy = {"helper": {"permissions": ["now", "write_file"], "scopes": {
            "write_file": {"auto_paths": ["*"]}}}}  # pre-allowed for clean runs; chat runs are tainted
        answer = aios.make_bot_answerer(k, "helper", policy, aios.Budget(max_total_tokens=5000))
        self.assertEqual(await answer("Their message: write x.txt", "1"), "I can't do that.")
        self.assertFalse((aios.WORKSPACE / "x.txt").exists())
        statuses = [e["status"] for e in k.store.audit_tail("helper") if e["tool"] == "write_file"]
        self.assertTrue(statuses and all(s != "ok" for s in statuses))

    async def test_default_tools_without_policy(self):
        k = self.kernel(text("hi"))
        answer = aios.make_bot_answerer(k, "main", None, None)
        self.assertEqual(await answer("hello", "1"), "hi")
        self.assertEqual(set(k.provider.calls[0]["tools"]), set(aios.SERVE_DEFAULT_TOOLS) & set(k.tools.names()))
        self.assertIn("answering people in a chat", k.provider.calls[0]["system"])

    async def test_daily_limit(self):
        k = self.kernel(aios.Completion([{"type": "text", "text": "a"}], "end_turn", 300, 300))
        answer = aios.make_bot_answerer(k, "main", None, None, daily_tokens=500)
        await answer("one", "1")
        with self.assertRaises(aios.DailyLimitReached):
            await answer("two", "1")

    async def test_bot_cmd_runs_against_a_fake_telegram(self):
        api = FakeAPI()
        api.post("@aios_bot hello")

        real = api.__call__
        polls = []

        def stopping(method, params):
            if method == "getUpdates":
                polls.append(1)
                if len(polls) > 2:
                    raise KeyboardInterrupt
            return real(method, params)
        provider = Scripted(text("Hello Alice!"))
        rc = await aios.bot_cmd("main", None, self.home, provider, None, {GROUP}, api=stopping)
        self.assertEqual(rc, 0)
        self.assertEqual([p["text"] for p in api.sent()], ["Hello Alice!"])
        self.assertTrue(any(e["agent"] == "main" for e in aios.Store(os.path.join(self.home, "aios.db")).audit_tail()))


if __name__ == "__main__":
    unittest.main()
