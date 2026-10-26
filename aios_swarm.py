"""Agent swarms: the engine behind the `swarm` tool and `aios swarm`.

Many sub-tasks run at once through a fixed pool of workers that pull from the task list, so a list
of any length (even a file read line by line) never holds more than `parallel` runs in flight and
input is only read when a worker is free (backpressure). A Ledger caps the tokens and cost that all
workers of one swarm spend together; nested swarms charge their parent's ledger too. The kernel's
permissions, scopes, approvals, taint and audit apply to every worker exactly as to one delegation:
this module only schedules, it never decides what a worker may do.

One Python process runs every worker on one core (~2,300 agents/s with an instant model). With
workers > 1, fan_out_procs spreads them over that many processes, one event loop each, fed from one
queue; a SharedLedger keeps the budget shared across all of them.
"""
import asyncio
import multiprocessing
import queue
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

MIN_REPLY_TOKENS = 64  # same as aios.MIN_REPLY_TOKENS

RESULT_CHARS = 1_500  # chars of one worker's answer shown to the calling agent
ANSWER_CHARS = 20_000  # chars of the whole swarm answer shown to the calling agent
REDUCE_ITEM_CHARS = 4_000  # chars of one answer handed to a reducer
REDUCE_GROUP_CHARS = 16_000  # chars handed to one reducer run (so each round shrinks ~4x)
REDUCE_MAX_ROUNDS = 6


class Ledger:
    """Tokens (and cost) spent by every run that holds it, summed. Checked before and after each
    model call, so a swarm stops starting work once the shared limit is reached; calls already in
    flight can overshoot by at most one call each."""

    def __init__(self, max_tokens: int | None = None, max_cost_usd: float | None = None,
                 price_in_per_mtok: float = 0.0, price_out_per_mtok: float = 0.0, parent=None):
        if max_cost_usd is not None and not (price_in_per_mtok or price_out_per_mtok):
            raise ValueError("a swarm cost limit needs --price-in / --price-out")
        self.max_tokens, self.max_cost_usd = max_tokens, max_cost_usd
        self.price_in, self.price_out = price_in_per_mtok, price_out_per_mtok
        self.parent = parent
        self.tokens_in = self.tokens_out = self.calls = 0

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def cost(self) -> float:
        return (self.tokens_in * self.price_in + self.tokens_out * self.price_out) / 1e6

    def add(self, tokens_in: int, tokens_out: int):
        led = self
        while led is not None:
            led._add(tokens_in, tokens_out)
            led = led.parent

    def _add(self, tokens_in: int, tokens_out: int):
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.calls += 1

    def output_room(self, next_in: int, room: int) -> int:
        """Like Budget.output_room, for this ledger and every one above it: room, or less."""
        led = self
        while led is not None:
            if led.max_tokens is not None:
                room = min(room, led.max_tokens - led.tokens - next_in)
            if led.max_cost_usd is not None:
                left = led.max_cost_usd - (led.cost() + next_in * led.price_in / 1e6)
                room = min(room, int(left * 1e6 / led.price_out) if led.price_out else room if left > 0 else 0)
            led = led.parent
        return max(room, 0)

    def exceeded(self) -> str | None:
        led = self
        while led is not None:
            # exhausted once too little is left for one more reply (aios.MIN_REPLY_TOKENS)
            if led.max_tokens is not None and led.tokens > led.max_tokens - MIN_REPLY_TOKENS:
                return f"swarm token budget exhausted ({led.tokens}/{led.max_tokens})"
            if led.max_cost_usd is not None and led.cost() >= led.max_cost_usd:
                return f"swarm cost budget exhausted (${led.cost():.4f}/${led.max_cost_usd:.4f})"
            led = led.parent
        return None


class SharedLedger(Ledger):
    """A Ledger whose counters live in shared memory, so worker processes spend from one budget.
    Create it before starting the processes and hand it to them as a Process argument."""

    def __init__(self, max_tokens: int | None = None, max_cost_usd: float | None = None,
                 price_in_per_mtok: float = 0.0, price_out_per_mtok: float = 0.0, ctx=None):
        self._v = (ctx or multiprocessing.get_context("spawn")).Array("q", 3)  # in, out, calls
        super().__init__(max_tokens, max_cost_usd, price_in_per_mtok, price_out_per_mtok)

    def _get(i):
        return property(lambda self: self._v[i], lambda self, x: self._v.__setitem__(i, x))

    tokens_in, tokens_out, calls = _get(0), _get(1), _get(2)
    del _get

    def _add(self, tokens_in: int, tokens_out: int):
        with self._v.get_lock():  # += on shared memory is not atomic across processes
            self._v[0] += tokens_in
            self._v[1] += tokens_out
            self._v[2] += 1


async def fan_out(items, work, parallel: int, on_result, stop=None, timeout: float | None = None):
    """Run `await work(item) -> (status, text)` for every item, at most `parallel` at a time.
    `items` may be any iterable (a generator is read lazily) or async iterable. `on_result(i, item,
    status, text)` is awaited as each one finishes, in completion order. `stop()` -> reason: once it
    returns one, the remaining items are reported as "skipped" without running. Cancelling fan_out
    cancels every worker."""
    if hasattr(items, "__aiter__"):
        it = _enumerate_async(items)
    else:
        it = _AsyncIter(enumerate(items))  # one shared iterator: next() never runs concurrently

    async def worker():
        async for i, item in it:
            why = stop() if stop else None
            if why:
                await on_result(i, item, "skipped", why)
                continue
            try:
                job = work(item)
                status, text = await (asyncio.wait_for(job, timeout) if timeout else job)
            except asyncio.TimeoutError:
                status, text = "timeout", f"no answer within {timeout:g}s"
            except Exception as e:
                status, text = "error", f"{type(e).__name__}: {e}"
            await on_result(i, item, status, text)

    tasks = [asyncio.create_task(worker()) for _ in range(max(1, int(parallel)))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class _AsyncIter:
    def __init__(self, it):
        self.it = it

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.it)
        except StopIteration:
            raise StopAsyncIteration from None


class _enumerate_async:
    """enumerate() for an async iterable that many workers read: one of them at a time waits."""

    def __init__(self, items):
        self.it, self.n, self.lock = items.__aiter__(), 0, asyncio.Lock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        async with self.lock:
            item = await self.it.__anext__()  # StopAsyncIteration ends every worker in turn
            self.n += 1
            return self.n - 1, item


def share(total: int, n: int, workers: int) -> int:
    """Worker n's part of `total` split over `workers` (the parts add up to total, each >= 1)."""
    return max(1, total // workers + (1 if n < total % workers else 0))


# ---- many processes ----
# Each worker process has its own queue. The parent knows how many items every worker holds, so it
# reads items only when a worker has a free slot and sends that worker as many as it can start
# (in one message: one message per item would make the parent the bottleneck). Workers send
# (i, status, text) back in batches too. A worker that dies takes exactly the items it held.
# Items and results must pickle (a swarm's jobs are tuples of strings). Processes are spawned,
# never forked: the same on Linux, macOS and Windows, and no copy of the parent's threads, sockets
# or database connection.
READY, DONE, FAILED, RESULTS, AUDIT = "ready", "done", "failed", "results", "audit"


def worker_started():
    """Call first in a worker process: Ctrl+C reaches every process of a console, and the parent
    is the one that decides (it stops the workers itself)."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


async def queue_items(task_q, poll: float = 0.2):
    """Worker side: the (i, item) pairs the parent sends, until it sends None."""
    loop = asyncio.get_running_loop()
    stop = threading.Event()

    def get():  # the next batch, and any others already waiting (one thread hop for all)
        while not stop.is_set():
            try:
                out = [task_q.get(timeout=poll)]
                break
            except queue.Empty:
                pass
        else:
            return [None]
        while out[-1] is not None:
            try:
                out.append(task_q.get_nowait())
            except queue.Empty:
                break
        return out

    pool = ThreadPoolExecutor(1, thread_name_prefix="aios-swarm-in")
    try:
        while True:
            for batch in await loop.run_in_executor(pool, get):
                if batch is None:
                    return
                for pair in batch:
                    yield pair
    finally:
        stop.set()
        pool.shutdown(wait=False)


class ResultSender:
    """Worker side: send(i, status, text) and audit(row) are buffered, in order, and flushed every
    `every` s or `size` entries."""

    def __init__(self, result_q, n: int, size: int = 64, every: float = 0.005):
        self.q, self.n, self.size, self.every, self.buf, self.task = result_q, n, size, every, [], None

    def send(self, i, status, text):
        self._add((i, status, text))

    def audit(self, row: tuple):
        self._add((None, AUDIT, row))

    def _add(self, entry):
        self.buf.append(entry)
        if len(self.buf) >= self.size:
            self.flush()

    def flush(self):
        if self.buf:
            self.q.put((RESULTS, self.n, self.buf))
            self.buf = []

    async def _tick(self):
        while True:
            await asyncio.sleep(self.every)
            self.flush()

    async def __aenter__(self):
        self.task = asyncio.create_task(self._tick())
        return self

    async def __aexit__(self, *exc):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.flush()


async def fan_out_procs(items, procs: list, on_result, stop=None, batch: int = 32, on_audit=None,
                        poll: float = 0.2):
    """Run every item in worker processes. procs: one (target, args, slots) per process; each is
    started as target(task_q, result_q, n, *args), runs up to `slots` items at once and must put
    (READY, n, None) on result_q once set up (or (FAILED, n, reason) and exit), then run the items
    of queue_items(task_q), report them through ResultSender(result_q, n) and finally put
    (DONE, n, None). If any worker cannot start, nothing runs and this raises RuntimeError.
    Items are read only when a worker has a free slot (at most `batch` sent at once), as fan_out
    reads them when a coroutine is free. on_result and stop as in fan_out (stop is checked here
    before an item is sent, and should be in the workers too). A worker that dies takes the items
    it held with it: they are reported as "error". Cancelling stops every process.
    on_audit(rows): the rows workers sent with ResultSender.audit, in their order and before the
    results sent with them (one process writes them all: SQLite serializes writers, and several
    processes taking turns on one file is slower than one)."""
    ctx = multiprocessing.get_context("spawn")
    qs, result_q = [ctx.Queue() for _ in procs], ctx.Queue()
    ps = [ctx.Process(target=t, args=(qs[n], result_q, n, *a), daemon=True) for n, (t, a, _) in enumerate(procs)]
    slots = [max(1, int(s)) for _, _, s in procs]
    held = [set() for _ in procs]  # items sent to each worker and not answered yet
    loop = asyncio.get_running_loop()
    results: asyncio.Queue = asyncio.Queue()
    pending, ended, room = {}, {}, asyncio.Event()  # ended: worker -> why ("" = done)
    closing = threading.Event()

    def reader():  # results arrive on a thread; the event loop handles them
        post = lambda msg: loop.call_soon_threadsafe(results.put_nowait, msg)
        last = time.monotonic()
        while not closing.is_set():
            try:
                post(result_q.get(timeout=poll))
            except queue.Empty:
                pass
            except (EOFError, OSError):
                return
            if time.monotonic() - last >= poll:
                last = time.monotonic()
                if dead := [n for n, p in enumerate(ps) if not p.is_alive()]:
                    try:  # a process that has exited sent everything it will: drain, then report
                        while True:
                            post(result_q.get_nowait())
                    except queue.Empty:
                        pass
                    post(("dead", dead, None))

    async def feed():
        it = iter(enumerate(items))
        while True:
            live = [n for n in range(len(ps)) if n not in ended]
            if not live:
                for i, item in it:
                    await on_result(i, item, "error", "no worker process left: " + next(iter(ended.values())))
                return
            w = max(live, key=lambda n: slots[n] - len(held[n]))
            free = slots[w] - len(held[w])
            if free <= 0:
                room.clear()
                await room.wait()
                continue
            chunk = []
            for i, item in it:
                why = stop() if stop else None
                if why:
                    await on_result(i, item, "skipped", why)
                    continue
                pending[i] = item
                held[w].add(i)
                chunk.append((i, item))
                if len(chunk) >= min(batch, free):
                    break
            if not chunk:
                return
            qs[w].put(chunk)

    async def deliver(n, entries):
        rows = [text for i, _, text in entries if i is None]
        if rows and on_audit:  # one transaction per batch, before any of its results
            on_audit(rows)
        for i, status, text in entries:
            if i is not None:
                held[n].discard(i)
                await on_result(i, pending.pop(i), status, text)
        room.set()

    async def lose(n, why):
        ended[n] = why
        for i in sorted(held[n]):
            await on_result(i, pending.pop(i), "error", f"worker process died: {why}")
        held[n].clear()
        room.set()

    for p in ps:
        p.start()
    rt = threading.Thread(target=reader, name="aios-swarm-out", daemon=True)
    rt.start()
    feeder = None
    try:
        ready = set()
        while len(ready) < len(ps):  # every worker set up (policy, model, tools) before any item
            kind, a, b = await results.get()
            if kind == READY:
                ready.add(a)
            elif kind == FAILED:
                raise RuntimeError(f"swarm worker {a} could not start: {b}")
            elif kind == "dead" and (dead := [n for n in a if n not in ready]):
                raise RuntimeError(f"swarm worker exited with code {ps[dead[0]].exitcode} while starting")
        feeder = asyncio.create_task(feed())
        stopping = False
        while len(ended) < len(ps):
            if not stopping and feeder.done():
                feeder.result()  # raises what the feeder raised
                stopping = True
                for q in qs:
                    q.put(None)  # no more items: each worker ends when its own are done
            get = asyncio.ensure_future(results.get())
            await asyncio.wait([get, feeder] if not stopping else [get], return_when=asyncio.FIRST_COMPLETED)
            if not get.done():
                get.cancel()
                continue
            kind, a, b = get.result()
            if kind == RESULTS:
                await deliver(a, b)
            elif kind == DONE and a not in ended:
                ended[a] = ""
            elif kind == "dead":
                for n in a:
                    if n not in ended:
                        await lose(n, f"exit code {ps[n].exitcode}")
    finally:
        closing.set()
        if feeder:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        for p in ps:
            if p.is_alive():
                p.terminate()
        for p in ps:
            await loop.run_in_executor(None, p.join, 5)
        await loop.run_in_executor(None, rt.join, poll * 5)
        result_q.close()
        for q in qs:
            q.cancel_join_thread()
            q.close()


def clip(text: str, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[:n] + f"... [{len(text) - n} more chars]"


def pack(texts: list[str], limit: int | None = None) -> list[list[str]]:
    """Split answers into groups of at most `limit` chars (at least one answer per group)."""
    limit = limit or REDUCE_GROUP_CHARS
    groups, cur, size = [], [], 0
    for t in texts:
        if cur and size + len(t) > limit:
            groups.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += len(t)
    if cur:
        groups.append(cur)
    return groups


def summary_line(counts: dict, n: int, parallel: int, ledger: Ledger) -> str:
    parts = ", ".join(f"{s} {c}" for s, c in sorted(counts.items()))
    cost = f", ${ledger.cost():.4f}" if ledger.price_in or ledger.price_out else ""
    return (f"swarm of {n} task(s), up to {parallel} at once: {parts or 'nothing ran'}; "
            f"{ledger.tokens} tokens in {ledger.calls} model call(s){cost}")
