"""Agent swarms: the engine behind the `swarm` tool and `aios swarm`.

Many sub-tasks run at once through a fixed pool of workers that pull from the task list, so a list
of any length (even a file read line by line) never holds more than `parallel` runs in flight and
input is only read when a worker is free (backpressure). A Ledger caps the tokens and cost that all
workers of one swarm spend together; nested swarms charge their parent's ledger too. The kernel's
permissions, scopes, approvals, taint and audit apply to every worker exactly as to one delegation:
this module only schedules, it never decides what a worker may do.
"""
import asyncio

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
            led.tokens_in += tokens_in
            led.tokens_out += tokens_out
            led.calls += 1
            led = led.parent

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


async def fan_out(items, work, parallel: int, on_result, stop=None, timeout: float | None = None):
    """Run `await work(item) -> (status, text)` for every item, at most `parallel` at a time.
    `items` may be any iterable (a generator is read lazily). `on_result(i, item, status, text)` is
    awaited as each one finishes, in completion order. `stop()` -> reason: once it returns one, the
    remaining items are reported as "skipped" without running. Cancelling fan_out cancels every worker."""
    it = enumerate(items)

    async def worker():
        for i, item in it:  # one shared iterator: next() never runs concurrently in one event loop
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
