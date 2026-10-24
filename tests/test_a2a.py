"""A2A: serving an agent over the Agent2Agent protocol and calling remote agents as tools."""
import asyncio
import http.client
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_a2a  # noqa: E402

TOKEN = "s3cret-token"


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


def user_msg(t="hello"):
    return {"kind": "message", "role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": t}]}


class ServerCase(unittest.IsolatedAsyncioTestCase):
    """A Server whose 'agent' echoes; requests go through a real socket from a worker thread."""

    async def asyncSetUp(self):
        self.seen = []

        async def run(agent, t):
            self.seen.append((agent, t))
            if t == "boom":
                raise RuntimeError("model down")
            if t == "slow":
                await asyncio.sleep(5)
            return f"answer to {t}"

        self.srv = aios_a2a.Server("helper", "Helps.", TOKEN, run, lambda: "agent-object",
                                   asyncio.get_running_loop(), port=0, timeout=0.5).start()
        self.addAsyncCleanup(asyncio.to_thread, self.srv.close)

    def _http(self, method, path, body=None, token=TOKEN, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, (json.loads(out) if out else None)

    async def http(self, *a, **kw):
        return await asyncio.to_thread(self._http, *a, **kw)

    def _sse(self, method, params):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.request("POST", "/", body=json.dumps({"jsonrpc": "2.0", "id": 9, "method": method, "params": params}),
                  headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}",
                           "Accept": "text/event-stream"})
        r = c.getresponse()
        kind, raw = r.getheader("Content-Type"), r.read().decode()
        c.close()
        events = [json.loads(chunk.removeprefix("data: ")) for chunk in raw.split("\n\n") if chunk.strip()]
        return kind, events

    async def sse(self, method, params):
        return await asyncio.to_thread(self._sse, method, params)

    async def rpc(self, method, params=None, **kw):
        status, body = await self.http("POST", "/", {"jsonrpc": "2.0", "id": 7, "method": method,
                                                     "params": params or {}}, **kw)
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], 7)
        return body


class ServerTests(ServerCase):
    async def test_card_is_public_at_both_well_known_paths(self):
        for path in aios_a2a.CARD_PATHS:
            status, card = await self.http("GET", path, token=None)
            self.assertEqual(status, 200)
            self.assertEqual((card["name"], card["protocolVersion"]), ("helper", aios_a2a.PROTOCOL))
            self.assertEqual(card["url"], f"http://127.0.0.1:{self.srv.port}/")
            self.assertTrue(card["capabilities"]["streaming"])
            self.assertEqual(card["security"], [{"bearer": []}])
        self.assertEqual(self.srv.httpd.server_address[0], "127.0.0.1")

    async def test_calls_need_the_token(self):
        for token in (None, "wrong"):
            status, _ = await self.http("POST", "/", {"jsonrpc": "2.0", "id": 1, "method": "message/send",
                                                      "params": {"message": user_msg()}}, token=token)
            self.assertEqual(status, 401)
        self.assertEqual(self.seen, [])

    async def test_send_returns_a_completed_task_that_tasks_get_finds(self):
        body = await self.rpc("message/send", {"message": user_msg("summarise")})
        task = body["result"]
        self.assertEqual((task["kind"], task["status"]["state"]), ("task", "completed"))
        self.assertEqual(aios_a2a.text_of(task["artifacts"][0]["parts"]), "answer to summarise")
        self.assertEqual(self.seen, [("agent-object", "summarise")])  # a fresh agent per request
        got = await self.rpc("tasks/get", {"id": task["id"]})
        self.assertEqual(got["result"], task)
        missing = await self.rpc("tasks/get", {"id": "nope"})
        self.assertEqual(missing["error"]["code"], aios_a2a.TASK_NOT_FOUND)

    async def test_failures_become_failed_tasks(self):
        for t, why in (("boom", "model down"), ("slow", "timed out")):
            task = (await self.rpc("message/send", {"message": user_msg(t)}))["result"]
            self.assertEqual(task["status"]["state"], "failed")
            self.assertIn(why, aios_a2a.text_of(task["status"]["message"]["parts"]))

    async def test_protocol_errors(self):
        cases = [
            ("message/stream", {}, aios_a2a.INVALID_PARAMS),  # checked before the stream starts
            ("tasks/cancel", {"id": "x"}, aios_a2a.TASK_NOT_FOUND),
            ("tasks/resubscribe", {"id": "x"}, aios_a2a.UNSUPPORTED),
            ("nope/nope", {}, aios_a2a.NO_METHOD),
            ("message/send", {}, aios_a2a.INVALID_PARAMS),
            ("message/send", {"message": dict(user_msg(), role="agent")}, aios_a2a.INVALID_PARAMS),
            ("message/send", {"message": dict(user_msg(), parts=[{"kind": "file", "file": {}}])}, aios_a2a.INVALID_PARAMS),
        ]
        for method, params, code in cases:
            with self.subTest(method=method, params=params):
                self.assertEqual((await self.rpc(method, params))["error"]["code"], code)
        status, body = await self.http("POST", "/", raw=b"{not json")
        self.assertEqual(body["error"]["code"], aios_a2a.PARSE)
        status, body = await self.http("POST", "/", {"jsonrpc": "1.0", "id": 1, "method": "message/send"})
        self.assertEqual(body["error"]["code"], aios_a2a.INVALID_REQUEST)
        status, _ = await self.http("POST", "/", raw=b"x" * (aios_a2a.BODY_MAX + 1))
        self.assertEqual(status, 413)
        self.assertEqual((await self.http("PUT", "/", {}))[0], 405)
        self.assertEqual((await self.http("POST", "/other", {}))[0], 404)
        self.assertEqual(self.seen, [])

    async def test_version_1_0_shapes(self):
        msg = {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "plan"}]}
        res = (await self.rpc("SendMessage", {"message": msg}))["result"]
        task = res["task"]  # 1.0 wraps the task; enum names, no "kind"
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(task["artifacts"][0]["parts"], [{"text": "answer to plan"}])
        self.assertNotIn("kind", task)
        self.assertEqual((await self.rpc("GetTask", {"id": task["id"]}))["result"], task)
        old = (await self.rpc("tasks/get", {"id": task["id"]}))["result"]  # same task, 0.3 shape
        self.assertEqual((old["kind"], old["status"]["state"]), ("task", "completed"))
        failed = (await self.rpc("SendMessage", {"message": dict(msg, parts=[{"text": "boom"}])}))["result"]["task"]
        self.assertEqual(failed["status"]["state"], "TASK_STATE_FAILED")
        self.assertEqual(failed["status"]["message"]["role"], "ROLE_AGENT")
        self.assertEqual((await self.rpc("CancelTask", {"id": "nope"}))["error"]["code"], aios_a2a.TASK_NOT_FOUND)
        self.assertEqual((await self.rpc("CreateTaskPushNotificationConfig", {}))["error"]["code"],
                         aios_a2a.UNSUPPORTED)
        _, card = await self.http("GET", aios_a2a.CARD_PATHS[0], token=None)
        self.assertEqual({i["protocolVersion"] for i in card["supportedInterfaces"]}, {"1.0", "0.3"})

    async def started(self, t):
        for _ in range(200):
            if any(seen == t for _, seen in self.seen):
                return
            await asyncio.sleep(0.01)
        self.fail(f"{t!r} never started")

    async def test_a_task_that_does_not_block_can_be_polled_and_canceled(self):
        self.srv.timeout = 30
        msg = dict(user_msg("slow"), contextId="c1")
        task = (await self.rpc("message/send", {"message": msg, "configuration": {"blocking": False}}))["result"]
        self.assertEqual(task["status"]["state"], "working")
        self.assertNotIn("artifacts", task)
        await self.started("slow")
        self.assertEqual((await self.rpc("tasks/get", {"id": task["id"]}))["result"]["status"]["state"], "working")
        body = await self.rpc("message/send", {"message": dict(user_msg(), taskId=task["id"])})
        self.assertIn("still running", body["error"]["message"])
        done = (await self.rpc("tasks/cancel", {"id": task["id"]}))["result"]
        self.assertEqual(done["status"]["state"], "canceled")
        self.assertEqual(aios_a2a.text_of(done["status"]["message"]["parts"]), aios_a2a.CANCELED)
        await asyncio.sleep(0.05)
        self.assertEqual((await self.rpc("tasks/get", {"id": task["id"]}))["result"]["status"]["state"], "canceled")
        self.assertNotIn("c1", self.srv.contexts)  # its history may end mid-step: forgotten
        self.assertEqual((await self.rpc("tasks/cancel", {"id": task["id"]}))["error"]["code"],
                         aios_a2a.NOT_CANCELABLE)
        quick = (await self.rpc("message/send", {"message": dict(msg, parts=[{"kind": "text", "text": "hi"}])}))["result"]
        self.assertEqual(quick["status"]["state"], "completed")
        self.assertEqual((await self.rpc("tasks/cancel", {"id": quick["id"]}))["error"]["code"],
                         aios_a2a.NOT_CANCELABLE)

    async def test_version_1_0_return_immediately_list_and_cancel(self):
        self.srv.timeout = 30
        msg = {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "slow"}], "contextId": "a"}
        res = (await self.rpc("SendMessage", {"message": msg, "configuration": {"returnImmediately": True}}))["result"]
        task = res["task"]
        self.assertEqual(task["status"]["state"], "TASK_STATE_WORKING")
        other = (await self.rpc("SendMessage", {"message": dict(msg, parts=[{"text": "hi"}], contextId="b"),
                                                "configuration": {"returnImmediately": True}}))["result"]["task"]
        listed = (await self.rpc("ListTasks", {}))["result"]
        self.assertEqual([t["id"] for t in listed["tasks"]], [other["id"], task["id"]])  # newest first
        self.assertEqual((listed["totalSize"], listed["nextPageToken"]), (2, ""))
        only_a = (await self.rpc("ListTasks", {"contextId": "a"}))["result"]["tasks"]
        self.assertEqual([t["id"] for t in only_a], [task["id"]])
        await self.started("slow")
        working = (await self.rpc("ListTasks", {"status": "TASK_STATE_WORKING"}))["result"]["tasks"]
        self.assertIn(task["id"], [t["id"] for t in working])
        for bad in ({"pageSize": 101}, {"pageSize": "5"}, {"pageToken": "next"}):
            self.assertEqual((await self.rpc("ListTasks", bad))["error"]["code"], aios_a2a.INVALID_PARAMS)
        done = (await self.rpc("CancelTask", {"id": task["id"]}))["result"]
        self.assertEqual(done["status"]["state"], "TASK_STATE_CANCELED")
        self.assertEqual(done["status"]["message"]["role"], "ROLE_AGENT")
        for _ in range(200):  # the queued one runs once the canceled one lets go
            got = (await self.rpc("GetTask", {"id": other["id"]}))["result"]
            if got["status"]["state"] != "TASK_STATE_WORKING":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(got["artifacts"][0]["parts"], [{"text": "answer to hi"}])

    async def test_unfinished_tasks_are_bounded(self):
        self.srv.timeout = 30
        with mock.patch.object(aios_a2a, "TASKS_ACTIVE", 1):
            later = {"blocking": False}
            first = (await self.rpc("message/send", {"message": user_msg("slow"), "configuration": later}))["result"]
            second = (await self.rpc("message/send", {"message": user_msg("hi"), "configuration": later}))["result"]
            self.assertEqual(second["status"]["state"], "rejected")
            await self.rpc("tasks/cancel", {"id": first["id"]})
            with mock.patch.object(aios_a2a, "TASKS_KEPT", 1):
                for _ in range(3):
                    await self.rpc("message/send", {"message": user_msg("hi")})
            self.assertEqual(len(self.srv.tasks), 1)

    async def test_streaming_sends_the_task_as_events(self):
        kind, events = await self.sse("message/stream", {"message": user_msg("stream me")})
        self.assertEqual(kind, "text/event-stream")
        self.assertTrue(all(e["jsonrpc"] == "2.0" and e["id"] == 9 for e in events))
        res = [e["result"] for e in events]
        self.assertEqual([r["kind"] for r in res], ["task", "status-update", "artifact-update", "status-update"])
        self.assertEqual(res[0]["status"]["state"], "submitted")
        self.assertEqual((res[1]["status"]["state"], res[1]["final"]), ("working", False))
        self.assertEqual(aios_a2a.text_of(res[2]["artifact"]["parts"]), "answer to stream me")
        self.assertEqual((res[3]["status"]["state"], res[3]["final"]), ("completed", True))
        self.assertEqual({r.get("taskId", r.get("id")) for r in res}, {res[0]["id"]})
        got = (await self.rpc("tasks/get", {"id": res[0]["id"]}))["result"]
        self.assertEqual(got["status"]["state"], "completed")  # the same task, findable afterwards

    async def test_streaming_in_version_1_0(self):
        msg = {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "boom"}]}
        _, events = await self.sse("SendStreamingMessage", {"message": msg})
        res = [e["result"] for e in events]
        self.assertEqual([next(iter(r)) for r in res], ["task", "statusUpdate", "statusUpdate"])
        self.assertEqual(res[0]["task"]["status"]["state"], "TASK_STATE_SUBMITTED")
        final = res[2]["statusUpdate"]["status"]
        self.assertEqual(final["state"], "TASK_STATE_FAILED")  # no answer artifact for a failure
        self.assertIn("model down", aios_a2a.text_of(final["message"]["parts"]))
        kind, events = await self.sse("SendStreamingMessage", {"message": dict(msg, role="ROLE_AGENT")})
        self.assertEqual((kind, events[0]["error"]["code"]), ("application/json", aios_a2a.INVALID_PARAMS))

    async def test_a_context_id_continues_the_conversation(self):
        made = []
        self.srv.make_agent = lambda: made.append(object()) or made[-1]
        first = (await self.rpc("message/send", {"message": user_msg("one")}))["result"]
        cid = first["contextId"]
        second = (await self.rpc("message/send", {"message": dict(user_msg("two"), contextId=cid)}))["result"]
        self.assertEqual(second["contextId"], cid)
        other = (await self.rpc("message/send", {"message": user_msg("three")}))["result"]
        self.assertNotEqual(other["contextId"], cid)
        self.assertEqual(len(made), 2)  # one agent per conversation
        self.assertIs(self.seen[0][0], self.seen[1][0])
        self.assertIsNot(self.seen[0][0], self.seen[2][0])
        mine = (await self.rpc("message/send", {"message": dict(user_msg("four"), contextId="my-ctx.1")}))["result"]
        self.assertEqual(mine["contextId"], "my-ctx.1")  # callers may name a new conversation

    async def test_context_rules(self):
        for bad in ("has space", "x" * 101, 5, "../.."):
            with self.subTest(bad=bad):
                body = await self.rpc("message/send", {"message": dict(user_msg(), contextId=bad)})
                self.assertEqual(body["error"]["code"], aios_a2a.INVALID_PARAMS)
        task = (await self.rpc("message/send", {"message": user_msg()}))["result"]
        body = await self.rpc("message/send", {"message": dict(user_msg(), taskId=task["id"])})
        self.assertEqual(body["error"]["code"], aios_a2a.UNSUPPORTED)  # finished: use its context
        body = await self.rpc("message/send", {"message": dict(user_msg(), taskId="nope")})
        self.assertEqual(body["error"]["code"], aios_a2a.TASK_NOT_FOUND)
        with mock.patch.object(aios_a2a, "CONTEXT_TURNS", 2):
            msg = dict(user_msg(), contextId="c")
            states = [(await self.rpc("message/send", {"message": msg}))["result"]["status"]["state"]
                      for _ in range(3)]
        self.assertEqual(states, ["completed", "completed", "rejected"])
        with mock.patch.object(aios_a2a, "CONTEXTS_KEPT", 2):
            for c in ("a", "b", "c2"):
                await self.rpc("message/send", {"message": dict(user_msg(), contextId=c)})
            self.assertEqual(list(self.srv.contexts), ["b", "c2"])  # the oldest is forgotten
        with mock.patch.object(aios_a2a, "CONTEXT_IDLE", -1):
            await self.rpc("message/send", {"message": dict(user_msg(), contextId="d")})
            self.assertEqual(list(self.srv.contexts), ["d"])  # idle ones are dropped

    async def test_the_client_continues_a_conversation(self):
        c = aios_a2a.Client(f"http://127.0.0.1:{self.srv.port}/", TOKEN, timeout=10)
        await asyncio.to_thread(c.card)
        for v1 in (True, False):
            c.v1 = v1
            with self.subTest(v1=v1):
                r = await asyncio.to_thread(c.ask, "hi")
                self.assertEqual((r["answer"], r["state"], r["task_id"]), ("answer to hi", "completed", None))
                r2 = await asyncio.to_thread(c.ask, "more", r["context_id"])
                self.assertEqual(r2["context_id"], r["context_id"])

    async def test_the_client_speaks_both_versions(self):
        for v1 in (True, False):
            c = aios_a2a.Client(f"http://127.0.0.1:{self.srv.port}/", TOKEN, timeout=10)
            await asyncio.to_thread(c.card)
            self.assertTrue(c.v1)  # the card offers 1.0
            c.v1 = v1
            with self.subTest(v1=v1):
                self.assertEqual(await asyncio.to_thread(c.send, "hi"), "answer to hi")

    async def test_the_client_round_trip(self):
        c = aios_a2a.Client(f"http://127.0.0.1:{self.srv.port}/", TOKEN, timeout=10)
        self.assertEqual((await asyncio.to_thread(c.card))["name"], "helper")
        self.assertEqual(await asyncio.to_thread(c.send, "hi"), "answer to hi")
        with self.assertRaisesRegex(RuntimeError, "task failed: RuntimeError: model down"):
            await asyncio.to_thread(c.send, "boom")
        bad = aios_a2a.Client(f"http://127.0.0.1:{self.srv.port}/", "wrong", timeout=10)
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            await asyncio.to_thread(bad.send, "hi")


class ClientCancelTests(unittest.TestCase):
    def test_a_task_that_takes_too_long_is_canceled(self):
        c = aios_a2a.Client("http://127.0.0.1:1/", timeout=0.05)
        working = {"kind": "task", "id": "t1", "status": {"state": "working"}}
        calls = []

        def rpc(method, params):
            calls.append((method, params))
            return working
        with mock.patch.object(c, "_rpc", side_effect=rpc):
            with self.assertRaisesRegex(RuntimeError, "asked the agent to cancel"):
                c.ask("hi", poll=0.01)
        self.assertEqual(calls[-1], ("tasks/cancel", {"id": "t1"}))


class ClientRuleTests(unittest.TestCase):
    def test_tokens_never_travel_over_plain_http_to_other_hosts(self):
        with self.assertRaisesRegex(ValueError, "use https"):
            aios_a2a.Client("http://agents.example.com/a2a", "t")
        aios_a2a.Client("https://agents.example.com/a2a", "t")
        aios_a2a.Client("http://agents.example.com/a2a")  # no token: allowed
        for local in ("http://127.0.0.1:8770/", "http://localhost:8770/", "http://[::1]:8770/"):
            aios_a2a.Client(local, "t")
        for bad in ("ftp://x/", "file:///etc/passwd", "http:///nohost"):
            with self.assertRaises(ValueError):
                aios_a2a.Client(bad)

    def test_state_names(self):
        for raw, want in (("TASK_STATE_INPUT_REQUIRED", "input-required"), ("completed", "completed"),
                          ("TASK_STATE_COMPLETED", "completed"), (None, "")):
            self.assertEqual(aios_a2a.state_name(raw), want)

    def test_text_of_ignores_other_parts(self):
        parts = [{"kind": "text", "text": "a"}, {"kind": "file", "file": {}}, {"kind": "text", "text": 5}, "x",
                 {"kind": "text", "text": "b"}]
        self.assertEqual(aios_a2a.text_of(parts), "a\nb")
        self.assertEqual(aios_a2a.text_of(None), "")


class KernelTests(unittest.IsolatedAsyncioTestCase):
    """A served aios agent, called by another aios kernel through the a2a__ tool."""
    POLICY = {"helper": {"permissions": {"write_file", "now"},
                         "scopes": {"write_file": {"paths": ["out/*"], "auto_paths": ["out/*"]}}}}

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = aios.WORKSPACE
        aios.WORKSPACE = Path(self._tmp.name).resolve()
        self.addCleanup(setattr, aios, "WORKSPACE", self._old)
        self.addCleanup(self._tmp.cleanup)
        self.remote_prov = Scripted()
        self.remote = aios.Kernel(store=aios.Store(":memory:"), provider=self.remote_prov, approver=None)
        self.addCleanup(self.remote.store.db.close)
        aios.install_builtin_tools(self.remote)
        self.srv = aios.make_a2a_server(self.remote, "helper", self.POLICY, None, TOKEN, port=0,
                                        description="Writes reports.")
        self.addAsyncCleanup(asyncio.to_thread, self.srv.close)
        self.url = f"http://127.0.0.1:{self.srv.port}/"

    async def test_served_runs_are_tainted_and_keep_no_history(self):
        self.remote_prov.script = [call("write_file", {"path": "out/a.txt", "content": "x"}), text("could not write"),
                                   text("second")]
        c = aios_a2a.Client(self.url, TOKEN, timeout=10)
        self.assertEqual(await asyncio.to_thread(c.send, "write out/a.txt"), "could not write")
        self.assertFalse((aios.WORKSPACE / "out/a.txt").exists())  # auto_paths do not apply to strangers
        (st,), = self.remote.store.db.execute("SELECT status FROM audit WHERE tool='write_file'").fetchall()
        self.assertEqual(st, "denied")
        self.assertEqual(await asyncio.to_thread(c.send, "again"), "second")
        self.assertEqual(len(self.remote_prov.calls[-1]["messages"]), 1)  # no memory of the first request
        self.assertEqual(self.remote.store.load_messages("helper"), [])  # nothing saved
        self.assertIn("not your user", self.remote_prov.calls[0]["system"])
        self.assertEqual(set(self.remote_prov.calls[0]["tools"]), {"write_file", "now"})

    async def test_the_a2a_tool_needs_approval_and_is_untrusted(self):
        self.remote_prov.script = [text("remote says hi")]
        k = aios.Kernel(store=aios.Store(":memory:"), provider=Scripted(), approver=None)
        self.addCleanup(k.store.db.close)
        with mock.patch.dict(os.environ, {"AIOS_A2A_TOKEN_HELPER": TOKEN}):
            self.assertEqual(await aios.install_a2a_tools(k, [f"helper={self.url}"]), ["a2a__helper"])
        self.assertIn("Writes reports.", k.tools._schemas["a2a__helper"]["description"])
        self.assertTrue(k.tools.is_untrusted("a2a__helper"))
        agent = aios.Agent("main", {"a2a__helper"})
        out, err, st = await k._execute(agent, "a2a__helper", {"task": "hi"}, False, {})
        self.assertEqual(st, "denied")  # nobody to ask: it sends data off the machine
        self.assertEqual(self.remote_prov.calls, [])

        async def yes(*a, **kw):
            return True

        k.approver = yes
        out, err, st = await k._execute(agent, "a2a__helper", {"task": "hi"}, False, {})
        self.assertEqual(st, "ok")
        self.assertRegex(out, r"^remote says hi\n\[conversation: [0-9a-f-]{36}\]$")

    async def test_the_a2a_tool_holds_a_multi_turn_conversation(self):
        self.remote_prov.script = [text("first"), text("second"), text("fresh")]
        k = aios.Kernel(store=aios.Store(":memory:"), provider=Scripted(), approver=None)
        self.addCleanup(k.store.db.close)
        with mock.patch.dict(os.environ, {"AIOS_A2A_TOKEN_HELPER": TOKEN}):
            await aios.install_a2a_tools(k, [f"helper={self.url}"])
        agent = aios.Agent("main", {"a2a__helper"})

        async def yes(*a, **kw):
            return True

        k.approver = yes
        out, _, _ = await k._execute(agent, "a2a__helper", {"task": "start"}, False, {})
        cid = out.rsplit("[conversation: ", 1)[1].rstrip("]")
        out2, _, st = await k._execute(agent, "a2a__helper", {"task": "go on", "conversation": cid}, False, {})
        self.assertEqual((out2.split("\n")[0], st), ("second", "ok"))
        self.assertEqual(len(self.remote_prov.calls[1]["messages"]), 3)  # the remote kept the first turn
        await k._execute(agent, "a2a__helper", {"task": "new"}, False, {})
        self.assertEqual(len(self.remote_prov.calls[2]["messages"]), 1)  # no id: a new conversation
        self.assertEqual(self.remote.store.load_messages("helper"), [])  # still never saved
        out, err, st = await k._execute(agent, "a2a__helper", {"task": "x", "conversation": "bad id"}, False, {})
        self.assertEqual(st, "error")

    async def test_bad_specs_and_unreachable_agents_fail_at_start(self):
        k = aios.Kernel(store=aios.Store(":memory:"), provider=Scripted(), approver=None)
        self.addCleanup(k.store.db.close)
        for spec in ("noequals", "bad name=http://x/", "x=ftp://x/"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                await aios.install_a2a_tools(k, [spec])
        with self.assertRaisesRegex(RuntimeError, "no A2A agent card"):
            await aios.install_a2a_tools(k, ["gone=http://127.0.0.1:9/"])


if __name__ == "__main__":
    unittest.main()
