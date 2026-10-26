"""Swarms in worker processes (`aios swarm --workers N`): one CPU core each, one shared budget, one audit chain."""
import asyncio
import functools
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aios  # noqa: E402
import aios_swarm as sw  # noqa: E402

POLICY = {"worker": {"permissions": set(), "scopes": {}}}


class PidModel:
    """Answers with the process it runs in; DIE kills that process, BOOM raises."""

    def __init__(self, tokens=10):
        self.tokens = tokens

    async def complete(self, system, messages, tools, max_tokens=1024):
        await asyncio.sleep(0.005)
        task = messages[0]["content"]
        if task.startswith("DIE"):
            os._exit(3)
        if task.startswith("BOOM"):
            raise RuntimeError("model down")
        return aios.Completion([{"type": "text", "text": f"pid {os.getpid()}"}], "end_turn", self.tokens, 0)


async def setup(home, tokens, n, workers):
    """worker_setup for these tests: runs in each worker process."""
    k = aios.Kernel(store=aios.Store(str(Path(home) / "aios.db")), provider=PidModel(tokens),
                    budget=aios.Budget(max_steps=2))
    aios.install_builtin_tools(k)
    aios.install_agent_tools(k, POLICY)
    caller = aios.Agent("swarm", {"agent__*"}, loaded=True, ephemeral=True)
    caller.run_budget = k.budget
    return k, caller


async def broken_setup(n, workers):
    raise ValueError(f"no policy here (worker {n})")


def count_worker(task_q, result_q, n, factor):
    """A bare fan_out_procs worker: answers i * factor."""
    sw.worker_started()

    async def main():
        result_q.put((sw.READY, n, None))
        async with sw.ResultSender(result_q, n, size=3) as out:
            async for i, item in sw.queue_items(task_q):
                out.audit(("row", i))
                out.send(i, "ok", str(item * factor))
        result_q.put((sw.DONE, n, None))

    asyncio.run(main())


def add_worker(task_q, result_q, n, ledger, times):
    sw.worker_started()
    for _ in range(times):
        ledger.add(1, 2)
    result_q.put((sw.READY, n, None))
    while task_q.get() is not None:
        pass
    result_q.put((sw.DONE, n, None))


class WorkerCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._old = aios.WORKSPACE
        aios.WORKSPACE = self.home / "workspace"
        aios.WORKSPACE.mkdir()
        self.addCleanup(setattr, aios, "WORKSPACE", self._old)

    def kernel(self):
        k = aios.Kernel(store=aios.Store(str(self.home / "aios.db")), provider=PidModel(),
                        budget=aios.Budget(max_steps=2))
        aios.install_builtin_tools(k)
        aios.install_agent_tools(k, POLICY)
        self.addCleanup(k.store.db.close)
        caller = aios.Agent("swarm", {"agent__*"}, loaded=True, ephemeral=True)
        caller.run_budget = k.budget
        return k, caller

    async def swarm(self, tasks, workers=2, tokens=10, parallel=8, setup_fn=None, **kw):
        k, caller = self.kernel()
        results = {}

        async def on_result(i, job, status, text):
            results[i] = (job[1], status, text)

        with mock.patch.object(aios, "SWARM_PARALLEL", parallel):
            res = await aios.run_swarm(k, caller, (("agent__worker", t, None) for t in tasks), parallel=parallel,
                                       on_result=on_result, workers=workers,
                                       worker_setup=setup_fn or functools.partial(setup, str(self.home), tokens),
                                       **kw)
        return k, res, results


class FanOutProcsTests(WorkerCase):
    async def test_every_item_comes_back_once_with_its_audit_rows_first(self):
        got, audited = {}, []

        async def done(i, item, status, text):
            self.assertIn(("row", i), audited)  # written before its result is reported
            got[i] = (item, status, text)

        await sw.fan_out_procs(range(50), [(count_worker, (10,), 3), (count_worker, (10,), 5)], done,
                               batch=4, on_audit=audited.extend)
        self.assertEqual(got, {i: (i, "ok", str(i * 10)) for i in range(50)})
        self.assertEqual(sorted(audited), [("row", i) for i in range(50)])

    async def test_input_is_read_only_for_free_slots(self):
        pulled, seen = [], []

        def items():
            for i in range(40):
                pulled.append(i)
                yield i

        async def done(i, item, status, text):
            seen.append(len(pulled))

        await sw.fan_out_procs(items(), [(count_worker, (1,), 2), (count_worker, (1,), 2)], done)
        self.assertLessEqual(seen[0], 4 + 1)  # both workers full, plus the one read that found no room
        self.assertEqual(len(seen), 40)

    async def test_stop_skips_what_is_not_sent_yet(self):
        got = {}

        async def done(i, item, status, text):
            got[i] = status

        await sw.fan_out_procs(range(10), [(count_worker, (1,), 1)], done, stop=lambda: "enough" if len(got) >= 3 else None)
        self.assertEqual(len(got), 10)
        self.assertIn("skipped", got.values())

    async def test_the_shared_ledger_sums_every_process(self):
        led = sw.SharedLedger(max_tokens=10_000)
        await sw.fan_out_procs([], [(add_worker, (led, 500), 1) for _ in range(3)], None)
        self.assertEqual((led.tokens_in, led.tokens_out, led.calls), (1500, 3000, 1500))
        inner = sw.Ledger(parent=led)
        inner.add(10, 0)
        self.assertEqual((inner.tokens, led.tokens), (10, 4510))
        self.assertIsNone(led.exceeded())
        led.add(6000, 0)
        self.assertIn("swarm token budget exhausted", inner.exceeded())

    def test_share_splits_exactly(self):
        self.assertEqual([sw.share(10, n, 3) for n in range(3)], [4, 3, 3])
        self.assertEqual([sw.share(2, n, 4) for n in range(4)], [1, 1, 1, 1])


class RunSwarmWorkersTests(WorkerCase):
    async def test_helpers_run_in_other_processes_on_one_audit_chain(self):
        k, res, results = await self.swarm([f"task {i}" for i in range(200)], workers=2)
        self.assertEqual(res["counts"], {"ok": 200})
        pids = {text for _, _, text in results.values()}
        self.assertEqual(len(pids), 2)
        self.assertNotIn(f"pid {os.getpid()}", pids)
        self.assertEqual(res["ledger"].tokens, 200 * 10)  # summed over both processes
        self.assertEqual(res["ledger"].calls, 200)
        rows = [e for e in k.store.audit_tail("swarm", 5000) if e["tool"] == "agent__worker"]
        self.assertEqual(len(rows), 200)
        self.assertEqual(len({json.loads(e["args"])["swarm"] for e in rows}), 1)  # one swarm id
        self.assertTrue(k.store.verify_audit()["ok"])
        self.assertIn("up to 8 at once", res["summary"])

    async def test_the_budget_is_shared_between_processes(self):
        with mock.patch.object(aios, "SWARM_MAX_TOKENS", 300):
            _, res, _ = await self.swarm([f"t{i}" for i in range(300)], workers=3, tokens=10, parallel=6)
        self.assertGreater(res["counts"].get("skipped", 0), 200)
        self.assertLess(res["counts"]["ok"], 60)  # ~25 fit; calls in flight may overshoot by one each

    async def test_failures_stay_per_task(self):
        _, res, results = await self.swarm(["BOOM", "fine", "also fine"], workers=2)
        self.assertEqual(res["counts"], {"error": 1, "ok": 2})
        self.assertIn("model down", next(t for task, s, t in results.values() if task == "BOOM"))

    async def test_a_worker_that_dies_takes_only_its_tasks(self):
        _, res, results = await self.swarm(["DIE"] + [f"t{i}" for i in range(40)], workers=2, parallel=2)
        self.assertEqual(len(results), 41)
        self.assertEqual(results[0][1], "error")
        self.assertIn("worker process died", results[0][2])
        self.assertGreater(res["counts"]["ok"], 0)

    async def test_a_worker_that_cannot_start_runs_nothing(self):
        with self.assertRaisesRegex(RuntimeError, "could not start: ValueError: no policy here"):
            await self.swarm(["a", "b"], setup_fn=broken_setup)

    async def test_nested_or_setup_less_swarms_stay_in_process(self):
        k, caller = self.kernel()
        with self.assertRaises(ValueError):
            await aios.run_swarm(k, caller, [("agent__worker", "x", None)], parallel=4, workers=2)
        res = await aios.run_swarm(k, caller, [("agent__worker", "x", None)], parallel=1, workers=4)
        self.assertEqual(res["counts"], {"ok": 1})  # workers are capped at parallel: 1 = in process


class FakeOpenAI(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class SwarmCliWorkersTests(WorkerCase):
    def test_aios_swarm_workers_end_to_end(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAI)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        (self.home / "p.toml").write_text('[agents.worker]\npermissions = []\n', encoding="utf-8")
        (self.home / "t.txt").write_text("".join(f"task {i}\n" for i in range(60)), encoding="utf-8")
        out = self.home / "o.jsonl"
        p = subprocess.run([sys.executable, str(ROOT / "aios.py"), "swarm", "--policy", str(self.home / "p.toml"),
                            "--tasks", str(self.home / "t.txt"), "--agent", "worker", "--out", str(out),
                            "--home", str(self.home), "--provider", "openai", "--model", "m",
                            "--base-url", f"http://127.0.0.1:{srv.server_port}/v1", "--workers", "2",
                            "--parallel", "6"],
                           capture_output=True, text=True, timeout=120, env={**os.environ, "OPENAI_API_KEY": ""})
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(r["line"] for r in rows), list(range(1, 61)))
        self.assertEqual({r["status"] for r in rows}, {"ok"})
        self.assertIn("swarm of 60 task(s), up to 6 at once: ok 60; 600 tokens in 60 model call(s)", p.stderr)
        store = aios.Store(str(self.home / "aios.db"))
        self.addCleanup(store.db.close)
        self.assertTrue(store.verify_audit()["ok"])
        self.assertEqual(len([e for e in store.audit_tail("swarm", 500) if e["tool"] == "agent__worker"]), 60)

    def test_workers_flag_is_checked(self):
        argv = ["aios", "swarm", "--policy", "p.toml", "--tasks", "t", "--workers", "-1"]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "stderr", io.StringIO()), \
                mock.patch.object(aios, "load_policy", return_value=POLICY), self.assertRaises(SystemExit) as cm:
            aios.cli()
        self.assertEqual(cm.exception.code, 2)
        self.assertGreaterEqual(aios.auto_workers(), 1)


if __name__ == "__main__":
    unittest.main()
