#!/usr/bin/env python3
"""How many agents can one AIOS process run at once? A ramp test of the real swarm path.

Every level runs N helper agents at the same time through `run_swarm` (the engine behind the
`swarm` tool and `aios swarm`): the kernel's permission checks, delegation, shared Ledger, model
slots and hash-chained audit log on a real SQLite file all run exactly as in production. Only the
model is chosen here:

  --model-kind mock       in-process fake model that sleeps --latency seconds (tests AIOS alone)
  --model-kind http-mock  fake OpenAI-compatible server in another process, called through the
                          real OpenAICompatProvider (adds HTTP, threads and sockets)
  --model-kind openai     a real server: Ollama, vLLM, llama.cpp... (--base-url, --model)

--workers N spreads the agents over N processes (one CPU core each, `aios swarm --workers`); the
default 1 is one process, one core. Each level runs in its own subprocess, so peak memory is per level. Output: one JSON line per
level (--out) and a Markdown table on stdout.

  python scripts/stress-swarm.py                       # 1, 2, 4, ... until something breaks
  python scripts/stress-swarm.py --factor 10 --max 100000   # 1, 10, 100, ... 100000
  python scripts/stress-swarm.py --model-kind openai --base-url http://localhost:8000/v1 \\
      --model Qwen/Qwen2.5-7B-Instruct --levels 16,64,256,1024
"""
import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import resource  # peak memory and the open-files limit; not on Windows
except ImportError:
    resource = None

ROOT = Path(__file__).resolve().parent.parent
POLICY = {"worker": {"permissions": set(), "scopes": {}}}


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


class MockProvider:
    """A model that thinks for `latency` seconds and answers in a few tokens."""

    def __init__(self, latency):
        self.latency = latency

    async def complete(self, system, messages, tools, max_tokens=1024):
        import aios
        await asyncio.sleep(self.latency)
        return aios.Completion([{"type": "text", "text": "done"}], "end_turn", 50, 5)


class Meter:
    """Wraps a provider: in-flight model calls (now / peak) and the time of each call."""

    def __init__(self, inner):
        self.inner, self.active, self.peak, self.calls = inner, 0, 0, []

    async def complete(self, *a, **kw):
        self.active += 1
        self.peak = max(self.peak, self.active)
        t = time.perf_counter()
        try:
            return await self.inner.complete(*a, **kw)
        finally:
            self.calls.append(time.perf_counter() - t)
            self.active -= 1


# ---- fake OpenAI-compatible server (http-mock) ----
def serve_mock(port, latency):
    reply = json.dumps({"choices": [{"message": {"role": "assistant", "content": "done"},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 50, "completion_tokens": 5}}).encode()

    async def handle(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            n = next((int(line.split(b":")[1]) for line in head.split(b"\r\n")
                      if line.lower().startswith(b"content-length:")), 0)
            await reader.readexactly(n)
            await asyncio.sleep(latency)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                         b"Content-Length: %d\r\n\r\n%s" % (len(reply), reply))
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def main():
        server = await asyncio.start_server(handle, "127.0.0.1", port, backlog=16384)
        print("ready", flush=True)
        async with server:
            await server.serve_forever()

    asyncio.run(main())


def make_provider(a, slots):
    import aios
    if a.model_kind == "mock":
        return MockProvider(a.latency)
    inner = aios.OpenAICompatProvider(a.model, a.base_url, os.environ.get("OPENAI_API_KEY"),
                                      timeout=int(a.timeout), temperature=0)
    inner.workers = slots  # the Kernel would set this, but it only sees the Meter
    return inner


async def worker_setup(a, home, n, workers):
    """One --workers process: the same kernel as run_level builds, on the same aios.db."""
    import aios
    aios.WORKSPACE = home / "workspace"
    slots = aios.MODEL_SLOTS  # this worker's share, set by aios.swarm_worker
    k = aios.Kernel(store=aios.Store(str(home / "aios.db")), provider=make_provider(a, slots),
                    budget=aios.Budget(max_steps=2), model_slots=slots)
    aios.install_builtin_tools(k)
    aios.install_agent_tools(k, POLICY)
    caller = aios.Agent("swarm", {"agent__*"}, loaded=True, ephemeral=True)
    caller.run_budget = k.budget
    return k, caller


# ---- one level, in this process ----
async def run_level(a):
    sys.path.insert(0, str(ROOT))
    import functools
    import aios

    n = a.level
    slots = a.model_slots or n
    aios.SWARM_PARALLEL = n
    aios.SWARM_MAX_TOKENS = 10**12
    aios.SWARM_TIMEOUT = a.timeout
    aios.MODEL_SLOTS = slots
    meter = Meter(make_provider(a, slots))
    home = Path(tempfile.mkdtemp(prefix="aios-stress-"))
    aios.WORKSPACE = home / "workspace"
    aios.WORKSPACE.mkdir()
    k = aios.Kernel(store=aios.Store(str(home / "aios.db")), provider=meter,
                    budget=aios.Budget(max_steps=2), model_slots=slots)
    aios.install_builtin_tools(k)
    aios.install_agent_tools(k, POLICY)
    caller = aios.Agent("swarm", {"agent__*"}, loaded=True, ephemeral=True)
    caller.run_budget = k.budget

    lag = [0.0]

    async def ticker():  # how late the event loop wakes up: >0.1s means it is saturated
        while True:
            t = time.perf_counter()
            await asyncio.sleep(0.05)
            lag[0] = max(lag[0], time.perf_counter() - t - 0.05)

    done_at, statuses, errors = [], {}, {}
    total = n * a.tasks_per_agent
    jobs = (("agent__worker", f"{a.prompt} (#{i})", i) for i in range(total))
    t0 = time.perf_counter()

    async def on_result(i, job, status, text):
        done_at.append(time.perf_counter() - t0)
        statuses[status] = statuses.get(status, 0) + 1
        if status != "ok":
            errors.setdefault(text[:160], 0)
            errors[text[:160]] += 1

    tick = asyncio.create_task(ticker())
    workers = a.workers or aios.auto_workers()
    setup = functools.partial(worker_setup, a, home) if workers > 1 else None
    cpu0 = time.process_time()
    try:
        await aios.run_swarm(k, caller, jobs, parallel=n, on_result=on_result, timeout=a.timeout,
                             workers=workers, worker_setup=setup)
    finally:
        tick.cancel()
    wall = time.perf_counter() - t0
    cpu = time.process_time() - cpu0
    if resource and workers > 1:  # the workers' CPU (their own model calls are not metered here)
        cpu += resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime + \
            resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime
    audit_rows = k.store.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    db_mb = sum(f.stat().st_size for f in home.glob("aios.db*")) / 1e6
    await k.close()
    k.store.db.close()
    ok = statuses.get("ok", 0)
    return {
        "agents": n, "tasks": total, "workers": min(workers, n), "model_kind": a.model_kind, "latency_s": a.latency,
        "model_slots": slots, "ok": ok, "failed": total - ok,
        "fail_rate": round((total - ok) / total, 4), "statuses": statuses,
        "top_errors": sorted(errors.items(), key=lambda e: -e[1])[:3],
        "wall_s": round(wall, 3), "tasks_per_s": round(ok / wall, 1) if wall else None,
        "peak_in_flight": meter.peak if workers == 1 else None,
        "task_p50_s": round(pct(done_at, 50), 3) if done_at else None,
        "task_p95_s": round(pct(done_at, 95), 3) if done_at else None,
        "task_p99_s": round(pct(done_at, 99), 3) if done_at else None,
        "model_call_p50_s": round(statistics.median(meter.calls), 3) if meter.calls else None,
        "model_call_p95_s": round(pct(meter.calls, 95), 3) if meter.calls else None,
        "cpu_s": round(cpu, 2), "cpu_ms_per_task": round(cpu * 1000 / max(total, 1), 2),
        "loop_lag_max_s": round(lag[0], 3),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 if sys.platform != "darwin"
                                                                                  else 1024 * 1024), 1)
        if resource else None,
        "worker_rss_mb": round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / (1024 if sys.platform != "darwin"
                                                                                       else 1024 * 1024), 1)
        if resource and workers > 1 else None,
        "audit_rows": audit_rows, "db_mb": round(db_mb, 1),
    }


RAMP_ONLY = ("levels", "level", "out", "serve_mock", "stop_fail_rate", "start", "factor", "max")


def levels(a):
    """Geometric ramp: start, start*factor, ... up to max (or the explicit --levels)."""
    if a.levels:
        return [int(x) for x in a.levels.split(",") if x.strip()]
    out, n = [], float(a.start)
    while round(n) <= a.max:
        if not out or round(n) > out[-1]:
            out.append(round(n))
        n *= a.factor
    return out


def table(rows):
    cols = [("agents", "agents"), ("workers", "workers"), ("ok", "ok"), ("fail_rate", "fail"), ("wall_s", "wall s"),
            ("tasks_per_s", "tasks/s"), ("peak_in_flight", "in flight"), ("task_p50_s", "p50 s"),
            ("task_p99_s", "p99 s"), ("model_call_p95_s", "model p95 s"), ("cpu_ms_per_task", "CPU ms/task"),
            ("loop_lag_max_s", "loop lag s"), ("peak_rss_mb", "RSS MB")]
    out = ["| " + " | ".join(h for _, h in cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "-")) for c, _ in cols) + " |")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=int, default=1, help="agents at once in the first level")
    ap.add_argument("--factor", type=float, default=2.0, help="each level multiplies the agents by this")
    ap.add_argument("--max", type=int, default=131072, help="last level (the ramp stops earlier on failures)")
    ap.add_argument("--levels", help="explicit levels instead of the ramp, e.g. 1,10,100,1000,10000")
    ap.add_argument("--model-kind", choices=["mock", "http-mock", "openai"], default="mock")
    ap.add_argument("--latency", type=float, default=1.0, help="seconds the fake model takes (mock kinds)")
    ap.add_argument("--base-url", default="http://localhost:11434/v1")
    ap.add_argument("--model", default="mock")
    ap.add_argument("--model-slots", type=int, default=0, help="model calls in flight (default: = agents)")
    ap.add_argument("--tasks-per-agent", type=int, default=1)
    ap.add_argument("--workers", type=int, default=1, help="processes (CPU cores) to use; 0 = one per core but one")
    ap.add_argument("--timeout", type=float, default=600.0, help="seconds one helper may run")
    ap.add_argument("--prompt", default="Reply with the single word: done.")
    ap.add_argument("--stop-fail-rate", type=float, default=0.5, help="stop ramping above this")
    ap.add_argument("--out", help="also append one JSON line per level here")
    ap.add_argument("--level", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--serve-mock", type=int, help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.serve_mock:
        return serve_mock(a.serve_mock, a.latency)
    if a.level:
        print(json.dumps(asyncio.run(run_level(a))), flush=True)
        return

    server = None
    if a.model_kind == "http-mock":
        port = 18000 + os.getpid() % 1000
        server = subprocess.Popen([sys.executable, __file__, "--serve-mock", str(port), "--latency", str(a.latency)],
                                  stdout=subprocess.PIPE, text=True)
        server.stdout.readline()
        a.base_url = f"http://127.0.0.1:{port}/v1"
    if resource:  # every HTTP call in flight holds a socket
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard if hard != resource.RLIM_INFINITY else 1 << 20, hard))
        except (ValueError, OSError):
            pass
    rows = []
    try:
        for level in levels(a):
            argv = [sys.executable, __file__, "--level", str(level)] + [
                x for k, v in vars(a).items() if k not in RAMP_ONLY
                for x in (f"--{k.replace('_', '-')}", str(v))]
            print(f"[stress] {level} agents...", file=sys.stderr, flush=True)
            p = subprocess.run(argv, capture_output=True, text=True)
            if p.returncode:
                row = {"agents": level, "crashed": p.stderr.strip()[-500:]}
            else:
                row = json.loads(p.stdout.strip().splitlines()[-1])
            rows.append(row)
            print(f"[stress] {json.dumps(row)}", file=sys.stderr, flush=True)
            if a.out:
                with open(a.out, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
            if "crashed" in row or row["fail_rate"] > a.stop_fail_rate:
                print(f"[stress] stopping at {level} agents", file=sys.stderr)
                break
    finally:
        if server:
            server.terminate()
    print(table(rows))


if __name__ == "__main__":
    main()
