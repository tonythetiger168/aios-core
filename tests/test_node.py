"""Multi-machine swarms (`aios swarm --nodes`, `aios node`): the head's queue, leases, heartbeats and
retries, the HTTP side, and whole swarms run by several nodes (here: in one process, over real HTTP)."""
import asyncio
import io
import shutil
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aios  # noqa: E402
import aios_node  # noqa: E402

TOKEN = "t" * 24
POLICY = {"worker": {"permissions": set(), "scopes": {}}, "reducer": {"permissions": set(), "scopes": {}}}


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class Model:
    """Answers 'answer to <task> from <tag>'; tasks starting with SLOW hang."""

    def __init__(self, tag, tokens=7):
        self.tag, self.tokens, self.seen = tag, tokens, []

    async def complete(self, system, messages, tools, max_tokens=1024):
        task = messages[0]["content"]
        self.seen.append(task)
        if task.startswith("SLOW"):
            await asyncio.sleep(30)
        if task.startswith("Combine"):
            return aios.Completion([{"type": "text", "text": f"combined {task.count('<untrusted')}"}],
                                   "end_turn", self.tokens, 0)
        await asyncio.sleep(0.005)
        return aios.Completion([{"type": "text", "text": f"answer to {task} from {self.tag}"}],
                               "end_turn", self.tokens, 3)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HubTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.hub = aios_node.Hub(lease_ttl=30, max_attempts=2, clock=self.clock)
        self.done = []

    def submit(self, task):
        return self.hub.submit("agent__worker", task, "s1", lambda *r: self.done.append((task, *r)))

    def test_lease_complete_and_first_answer_wins(self):
        a, b = self.submit("one"), self.submit("two")
        tasks, finished = self.hub.lease("n1", 1, 0)
        self.assertEqual([t["id"] for t in tasks], [a])
        self.assertEqual(tasks[0], {"id": a, "agent": "agent__worker", "task": "one", "swarm": "s1"})
        self.assertFalse(finished)
        self.assertEqual([t["id"] for t in self.hub.lease("n2", 5, 0)[0]], [b])
        self.assertTrue(self.hub.complete("n1", a, "ok", "A", 5, 2))
        self.assertFalse(self.hub.complete("n2", a, "ok", "again"))  # already answered
        self.assertEqual(self.done, [("one", "ok", "A", "n1", 5, 2)])
        st = self.hub.status()
        self.assertEqual((st["pending"], st["running"]), (0, 1))
        self.assertEqual({n["name"]: n["done"] for n in st["nodes"]}, {"n1": 1, "n2": 0})

    def test_a_quiet_node_loses_its_task_and_it_fails_after_max_attempts(self):
        a = self.submit("x")
        self.hub.lease("n1", 1, 0)
        self.clock.t += 20
        self.assertEqual(self.hub.heartbeat("n1", [a, "gone"]), ["gone"])  # kept alive; unknown id dropped
        self.clock.t += 20
        self.assertEqual(self.hub.lease("n2", 1, 0)[0], [])  # still n1's (heartbeat moved the deadline)
        self.clock.t += 31
        self.assertEqual([t["id"] for t in self.hub.lease("n2", 1, 0)[0]], [a])  # n1 went quiet
        self.assertEqual(self.hub.heartbeat("n1", [a]), [a])  # n1 is told to stop
        self.clock.t += 31
        self.hub.reap()  # n2 went quiet too: 2 attempts
        self.assertEqual(len(self.done), 1)
        self.assertEqual(self.done[0][1:4], ("error", "no answer: 2 nodes stopped answering while running it", "n2"))
        self.assertEqual({n["name"]: n["lost"] for n in self.hub.status()["nodes"]}, {"n1": 1, "n2": 1})

    def test_a_late_answer_from_the_first_node_still_counts(self):
        a = self.submit("x")
        self.hub.lease("n1", 1, 0)
        self.clock.t += 31
        self.hub.lease("n2", 1, 0)
        self.assertTrue(self.hub.complete("n1", a, "ok", "late but first"))
        self.assertEqual(self.hub.heartbeat("n2", [a]), [a])
        self.assertEqual(self.done[0][2], "late but first")

    def test_cancel_and_close(self):
        a = self.submit("x")
        self.hub.cancel(a)
        self.assertEqual(self.hub.lease("n1", 1, 0), ([], False))
        self.hub.close()
        self.assertEqual(self.hub.lease("n1", 1, 5), ([], True))  # no waiting once closed

    def test_a_long_poll_wakes_when_a_task_arrives(self):
        hub = aios_node.Hub()
        got = []
        t = threading.Thread(target=lambda: got.append(hub.lease("n1", 4, 5)))
        t.start()
        time.sleep(0.1)
        start = time.monotonic()
        hub.submit("agent__worker", "x", "s", lambda *r: None)
        t.join(5)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(got[0][0][0]["task"], "x")

    def test_listen_spec(self):
        self.assertEqual(aios_node.parse_listen("8780"), ("127.0.0.1", 8780))
        self.assertEqual(aios_node.parse_listen("0.0.0.0:1"), ("0.0.0.0", 1))
        self.assertEqual(aios_node.parse_listen("[::1]:9"), ("::1", 9))
        for bad in ("x", "1:99999", ""):
            with self.assertRaises(ValueError):
                aios_node.parse_listen(bad)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.hub = aios_node.Hub()
        self.srv = aios_node.HubServer(self.hub, TOKEN).start()
        self.addCleanup(self.srv.stop)

    def post(self, path, body, token=TOKEN, raw=None):
        req = urllib.request.Request(self.srv.url + path, data=raw if raw is not None else json.dumps(body).encode(),
                                     method="POST", headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_token_size_and_shape_are_checked(self):
        self.assertEqual(self.post("/node/lease", {"node": "n1"}, token="wrong" * 5)[0], 401)
        self.assertEqual(self.post("/node/lease", {"node": "bad name!"})[0], 400)
        self.assertEqual(self.post("/node/lease", None, raw=b"{")[0], 400)
        self.assertEqual(self.post("/node/heartbeat", {"node": "n1", "ids": "x"})[0], 400)
        self.assertEqual(self.post("/node/nope", {"node": "n1"})[0], 404)
        self.assertEqual(self.post("/node/result", None, raw=b"x" * (aios_node.BODY_MAX + 1))[0], 413)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.srv.url + "/node/status", timeout=10)
        self.assertEqual(cm.exception.code, 401)

    def test_round_trip_through_the_client(self):
        got = []
        self.hub.submit("agent__worker", "hello", "s", lambda *r: got.append(r))
        c = aios_node.HeadClient(self.srv.url, TOKEN)
        tasks, finished = c.lease("n1", 3, 0)
        self.assertEqual((len(tasks), finished), (1, False))
        self.assertEqual(c.heartbeat("n1", [tasks[0]["id"]]), [])
        self.assertTrue(c.result("n1", tasks[0]["id"], "ok", "hi", 4, 1))
        self.assertEqual(got, [("ok", "hi", "n1", 4, 1)])
        with self.assertRaises(PermissionError):
            aios_node.HeadClient(self.srv.url, "x" * 20).lease("n1", 1, 0)

    def test_safety_checks(self):
        with self.assertRaises(ValueError):  # the token would cross the network in plain text
            aios_node.HeadClient("http://10.0.0.5:8780", TOKEN)
        with self.assertRaises(ValueError):
            aios_node.HeadClient("http://127.0.0.1:8780", "")
        aios_node.HeadClient("https://10.0.0.5:8780", TOKEN)  # fine over https
        with self.assertRaises(ValueError):
            aios_node.HubServer(aios_node.Hub(), TOKEN, "0.0.0.0", 0)  # off loopback without TLS
        with self.assertRaises(ValueError):
            aios_node.HubServer(aios_node.Hub(), "short", "127.0.0.1", 0)


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a certificate")
class TlsTests(unittest.TestCase):
    def test_https_head_with_a_self_signed_certificate(self):
        with tempfile.TemporaryDirectory() as d:
            cert, key = str(Path(d) / "c.pem"), str(Path(d) / "k.pem")
            r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out",
                                cert, "-days", "1", "-subj", "/CN=localhost", "-addext",
                                "subjectAltName=IP:127.0.0.1"], capture_output=True)
            if r.returncode:
                self.skipTest("this openssl cannot make the certificate")
            hub = aios_node.Hub()
            srv = aios_node.HubServer(hub, TOKEN, "127.0.0.1", 0, cert, key).start()
            try:
                self.assertTrue(srv.url.startswith("https://"))
                got = []
                hub.submit("agent__worker", "hi", "s", lambda *r: got.append(r))
                c = aios_node.HeadClient(srv.url, TOKEN, cafile=cert)
                tasks, _ = c.lease("n1", 1, 0)
                self.assertTrue(c.result("n1", tasks[0]["id"], "ok", "yo"))
                self.assertEqual(got, [("ok", "yo", "n1", 0, 0)])
                with self.assertRaises(OSError):  # a node that does not trust the certificate stops there
                    aios_node.HeadClient(srv.url, TOKEN).lease("n1", 1, 0)
            finally:
                srv.stop()


class NodeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hub = aios_node.Hub(lease_ttl=0.6)
        self.srv = aios_node.HubServer(self.hub, TOKEN).start()
        self.client = aios_node.HeadClient(self.srv.url, TOKEN)

    async def asyncTearDown(self):
        await asyncio.to_thread(self.srv.stop)

    async def test_a_dead_node_s_task_goes_to_another_node(self):
        async def stuck(task):
            await asyncio.sleep(60)

        async def quick(task):
            return "ok", f"done {task['task']}", 1, 1

        quiet = lambda msg: None  # noqa: E731
        dead = asyncio.create_task(aios_node.serve_node(self.client, "dead", 4, stuck, heartbeat=0.1,
                                                        poll_wait=0.2, log=quiet))
        job = asyncio.create_task(self.hub.run("agent__worker", "x", "s"))
        for _ in range(100):
            if self.hub.status()["running"]:
                break
            await asyncio.sleep(0.02)
        dead.cancel()  # the machine is gone: no more heartbeats, no answer
        await asyncio.gather(dead, return_exceptions=True)
        alive = asyncio.create_task(aios_node.serve_node(self.client, "alive", 4, quick, once=True, heartbeat=0.1,
                                                         poll_wait=0.2, log=quiet))
        status, answer, node, *_ = await asyncio.wait_for(job, 10)
        self.assertEqual((status, answer, node), ("ok", "done x", "alive"))
        self.hub.close()
        counts = await asyncio.wait_for(alive, 10)
        self.assertEqual(counts, {"ok": 1})
        self.assertEqual({n["name"]: n["lost"] for n in self.hub.status()["nodes"]}, {"dead": 1, "alive": 0})

    async def test_a_task_the_head_no_longer_wants_is_stopped(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def run(task):
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                stopped.set()
                raise

        node = asyncio.create_task(aios_node.serve_node(self.client, "n1", 2, run, once=True, heartbeat=0.1,
                                                        poll_wait=0.2, log=lambda m: None))
        job = asyncio.create_task(self.hub.run("agent__worker", "x", "s"))
        await asyncio.wait_for(started.wait(), 10)
        job.cancel()  # e.g. the head's budget ran out
        await asyncio.wait_for(stopped.wait(), 10)  # the next heartbeat told the node
        self.hub.close()
        self.assertEqual(await asyncio.wait_for(node, 10), {"dropped": 1})

    async def test_once_gives_up_on_an_unreachable_head(self):
        c = aios_node.HeadClient(f"http://127.0.0.1:{free_port()}", TOKEN)
        counts = await asyncio.wait_for(aios_node.serve_node(c, "n1", 1, None, once=True, give_up=0.5,
                                                             log=lambda m: None), 10)
        self.assertEqual(counts, {})


class SwarmOverNodesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        patcher = mock.patch.object(aios, "WORKSPACE", aios.WORKSPACE)  # swarm_cmd / node_cmd set it
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_two_nodes_run_one_swarm(self):
        head, n1, n2 = (self.root / d for d in ("head", "n1", "n2"))
        for d in (head, n1, n2):
            d.mkdir()
        (head / "t.txt").write_text("".join(f"task {i}\n" for i in range(40)) + '{"agent": "ghost", "task": "x"}\n',
                                    encoding="utf-8")
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        models = {"n1": Model("n1"), "n2": Model("n2")}
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err), mock.patch.dict("os.environ", {"AIOS_NODE_TOKEN": TOKEN}), \
                mock.patch.object(aios, "SWARM_PARALLEL", 16):
            swarm = asyncio.create_task(aios.swarm_cmd(
                POLICY, str(head / "t.txt"), "worker", str(head / "out.jsonl"), str(head), Model("head"),
                aios.Budget(), "reducer", "Combine all.", nodes=str(port)))
            nodes = [asyncio.create_task(aios.node_cmd(url, TOKEN, name, POLICY, str(self.root / name), models[name],
                                                       aios.Budget(), parallel=4, once=True))
                     for name in ("n1", "n2")]
            code = await asyncio.wait_for(swarm, 60)
            node_codes = await asyncio.wait_for(asyncio.gather(*nodes), 60)
        self.assertEqual(code, 1, err.getvalue())  # the ghost line
        self.assertEqual(node_codes, [0, 0])
        rows = [json.loads(x) for x in (head / "out.jsonl").read_text(encoding="utf-8").splitlines()]
        answers = [r for r in rows if "n" in r]
        self.assertEqual(len(answers), 41)
        ok = [r for r in answers if r["status"] == "ok"]
        self.assertEqual(len(ok), 40)
        self.assertTrue(all(r["answer"].startswith(f"answer to task {r['line'] - 1} from n") for r in ok))
        self.assertEqual(rows[-1], {"combined": "combined 40", "status": "ok"})  # reduced on the head
        self.assertEqual(len(models["n1"].seen) + len(models["n2"].seen), 40)
        self.assertTrue(models["n1"].seen and models["n2"].seen, "both nodes should get work")
        self.assertIn("waiting for nodes", err.getvalue())
        # the head audits each task with the node that ran it; each node audits its own runs
        store = aios.Store(str(head / "aios.db"))
        rows = [e for e in store.audit_tail("swarm", 200) if e["tool"] == "agent__worker"]
        self.assertEqual(len(rows), 40)
        self.assertEqual({json.loads(e["args"])["node"] for e in rows}, {"n1", "n2"})
        self.assertTrue(store.verify_audit()["ok"])
        store.db.close()
        for name in ("n1", "n2"):
            store = aios.Store(str(self.root / name / "aios.db"))
            mine = [e for e in store.audit_tail("node", 200) if e["tool"] == "agent__worker"]
            self.assertEqual(len(mine), len(models[name].seen))
            store.db.close()

    async def test_the_head_counts_the_nodes_tokens_against_its_budget(self):
        head, n1 = self.root / "head", self.root / "n1"
        head.mkdir()
        n1.mkdir()
        (head / "t.txt").write_text("".join(f"task {i}\n" for i in range(30)), encoding="utf-8")
        port = free_port()
        with mock.patch.object(sys, "stderr", io.StringIO()), mock.patch.dict("os.environ", {"AIOS_NODE_TOKEN": TOKEN}), \
                mock.patch.object(aios, "SWARM_PARALLEL", 1), mock.patch.object(aios, "SWARM_MAX_TOKENS", 100):
            swarm = asyncio.create_task(aios.swarm_cmd(POLICY, str(head / "t.txt"), "worker", str(head / "o.jsonl"),
                                                       str(head), Model("head"), aios.Budget(), nodes=str(port)))
            node = asyncio.create_task(aios.node_cmd(f"http://127.0.0.1:{port}", TOKEN, "n1", POLICY, str(n1),
                                                     Model("n1", tokens=20), aios.Budget(), parallel=1, once=True))
            await asyncio.wait_for(swarm, 60)
            await asyncio.wait_for(node, 60)
        statuses = [json.loads(x)["status"] for x in (head / "o.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(statuses), 30)
        self.assertGreater(statuses.count("skipped"), 20)  # 23 tokens a task: the head stopped handing out
        self.assertLess(statuses.count("ok"), 6)

    async def test_a_node_refuses_an_agent_its_policy_lacks(self):
        hub = aios_node.Hub()
        srv = aios_node.HubServer(hub, TOKEN).start()
        try:
            node = asyncio.create_task(aios.node_cmd(srv.url, TOKEN, "n1", {"other": POLICY["worker"]},
                                                     str(self.root), Model("n1"), aios.Budget(), once=True))
            with mock.patch.object(sys, "stderr", io.StringIO()):
                status, answer, *_ = await asyncio.wait_for(hub.run("agent__worker", "x", "s"), 30)
                hub.close()
                await asyncio.wait_for(node, 30)
            self.assertEqual(status, "error")
            self.assertIn("no agent 'agent__worker' in this node's policy", answer)
        finally:
            await asyncio.to_thread(srv.stop)


class CliTests(unittest.TestCase):
    def run_cli(self, *args, env=None):
        import os
        e = {k: v for k, v in os.environ.items() if k != "AIOS_NODE_TOKEN"}
        e.update(env or {})
        return subprocess.run([sys.executable, str(ROOT / "aios.py"), *args], capture_output=True, text=True,
                              timeout=60, env=e, cwd=tempfile.gettempdir())

    def test_flag_checks(self):
        pol = str(ROOT / "policy.example.toml")
        base = ["swarm", "--policy", pol, "--tasks", "-", "--provider", "openai", "--model", "m"]
        r = self.run_cli(*base, "--nodes", "0.0.0.0:8780")
        self.assertEqual(r.returncode, 2)
        self.assertIn("needs --node-cert", r.stderr)
        r = self.run_cli(*base, "--nodes", "8780", "--workers", "2")
        self.assertIn("do not mix", r.stderr)
        r = self.run_cli(*base, "--node-cert", "c.pem")
        self.assertIn("go with --nodes", r.stderr)
        r = self.run_cli("node", "--head", "http://10.1.2.3:8780", "--policy", pol, "--provider", "openai",
                         "--model", "m", env={"AIOS_NODE_TOKEN": TOKEN})
        self.assertEqual(r.returncode, 2)
        self.assertIn("plain http", r.stderr)


if __name__ == "__main__":
    unittest.main()
