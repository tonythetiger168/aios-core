"""Nodes: one swarm spread over several machines (a head and workers, like a vLLM / Ray cluster).

    # head: the machine with the task file; it hands tasks out and collects the answers
    AIOS_NODE_TOKEN=<secret> aios swarm --policy p.toml --tasks tasks.txt --nodes 8780 --parallel 64
    # each worker machine (same policy file, its own model, sandbox and aios.db)
    AIOS_NODE_TOKEN=<secret> aios node --head http://127.0.0.1:8780 --policy p.toml --parallel 16

Workers pull: a node asks the head for as many tasks as it has free slots (a long poll), runs them
with its own kernel, and posts each answer back. So a worker needs no open port, can sit behind NAT,
and a faster or bigger machine simply takes more. While a node runs a task it sends a heartbeat; a
task whose node goes quiet for LEASE_TTL seconds goes back in the queue for another node, at most
MAX_ATTEMPTS times. The first answer wins; a node whose task was given away (or is no longer
wanted) is told on its next heartbeat and stops it.

Security model
- Every call needs the bearer token ($AIOS_NODE_TOKEN). Whoever holds it can hand this machine's
  agents tasks as the operator: share it only between your own machines.
- The head listens on 127.0.0.1 unless told otherwise; listening on another address needs a TLS
  certificate, and a node never sends the token over plain http to another machine. (Or keep the head
  on 127.0.0.1 behind an https reverse proxy or tunnel.)
- A node decides what runs: each task runs as the node's own policy agent, with its permissions,
  scopes, sandbox, approvals and budget, and is audited in the node's own aios.db. The head can only
  name an agent; an agent the node's policy lacks is refused. The head audits every task it handed
  out, with the node that answered.
- Answers are data. They come back as text, capped at ANSWER_MAX characters.
Standard library only.
"""
import asyncio
import collections
import hmac
import http.server
import json
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import aios_a2a

LEASE_TTL = 30.0  # seconds a node may go quiet before its tasks go to another node
HEARTBEAT = 10.0  # seconds between a node's heartbeats (well under LEASE_TTL)
MAX_ATTEMPTS = 3  # nodes one task may be given to before it fails
POLL_WAIT = 20.0  # seconds a node's lease request waits for work (long poll)
LEASE_MAX = 256  # tasks one lease request may take
BODY_MAX = 1024 * 1024  # bytes of one request or reply
ANSWER_MAX = 200_000  # chars of one answer
TASK_MAX = 200_000  # chars of one task
NODES_KEPT = 1000  # nodes remembered for status
NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


class Hub:
    """The head's queue. submit() / run() add tasks; nodes lease, heartbeat and complete them (from
    the HTTP server's threads). Thread-safe. on_done callbacks run with the hub's lock held, so they
    must be quick and must not call the hub (run() uses loop.call_soon_threadsafe)."""

    def __init__(self, lease_ttl: float = LEASE_TTL, max_attempts: int = MAX_ATTEMPTS, clock=time.monotonic):
        self.ttl, self.max_attempts, self.clock = lease_ttl, max_attempts, clock
        self.cv = threading.Condition()
        self.pending = collections.deque()  # job ids waiting for a node (may hold stale ids)
        self.jobs = {}  # id -> {"tool", "task", "sid", "attempts", "node", "deadline", "on_done"}
        self.nodes = collections.OrderedDict()  # name -> {"seen", "done", "lost"}
        self.closed = False

    def submit(self, tool: str, task: str, sid: str, on_done) -> str:
        """on_done(status, answer, node, tokens_in, tokens_out) is called once."""
        jid = secrets.token_hex(8)
        with self.cv:
            self.jobs[jid] = {"tool": tool, "task": task, "sid": sid, "attempts": 0, "node": None,
                              "deadline": 0.0, "on_done": on_done}
            self.pending.append(jid)
            self.cv.notify_all()
        return jid

    async def run(self, tool: str, task: str, sid: str):
        """Hand one task to the nodes and wait: (status, answer, node, tokens_in, tokens_out)."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def done(*r):
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(r))

        jid = self.submit(tool, task, sid, done)
        try:
            return await fut
        except asyncio.CancelledError:
            self.cancel(jid)
            raise

    def cancel(self, jid: str):
        with self.cv:
            self.jobs.pop(jid, None)  # its id left in pending is skipped; its node is told to stop

    def _seen(self, node: str):
        n = self.nodes.pop(node, None) or {"seen": 0.0, "done": 0, "lost": 0}
        n["seen"] = self.clock()
        self.nodes[node] = n
        while len(self.nodes) > NODES_KEPT:
            self.nodes.popitem(last=False)

    def reap(self):
        """Tasks whose node went quiet go back in the queue, or fail after MAX_ATTEMPTS nodes."""
        with self.cv:
            now, back = self.clock(), []
            for jid, j in list(self.jobs.items()):
                if j["node"] is None or j["deadline"] > now:
                    continue
                if j["node"] in self.nodes:
                    self.nodes[j["node"]]["lost"] += 1
                if j["attempts"] >= self.max_attempts:
                    del self.jobs[jid]
                    j["on_done"]("error", f"no answer: {j['attempts']} nodes stopped answering while "
                                 "running it", j["node"], 0, 0)
                else:
                    j["node"] = None
                    back.append(jid)
            if back:
                self.pending.extendleft(reversed(back))  # first in line again
                self.cv.notify_all()

    def lease(self, node: str, n: int, wait: float = POLL_WAIT) -> tuple[list[dict], bool]:
        """Up to n tasks for `node`, waiting up to `wait` seconds for one. -> (tasks, finished):
        finished = the head has closed (no more tasks will come)."""
        end = self.clock() + max(0.0, wait)
        n = max(1, min(int(n), LEASE_MAX))
        with self.cv:
            while True:
                self._seen(node)
                self.reap()
                out = []
                while self.pending and len(out) < n:
                    jid = self.pending.popleft()
                    j = self.jobs.get(jid)
                    if j is None or j["node"] is not None:
                        continue
                    j["node"], j["deadline"] = node, self.clock() + self.ttl
                    j["attempts"] += 1
                    out.append({"id": jid, "agent": j["tool"], "task": j["task"], "swarm": j["sid"]})
                left = end - self.clock()
                if out or self.closed or left <= 0:
                    return out, self.closed
                self.cv.wait(min(left, max(self.ttl / 4, 0.05)))  # wake up now and then to reap

    def heartbeat(self, node: str, ids: list[str]) -> list[str]:
        """Keep `node`'s tasks; -> the ids it should stop (done elsewhere, given away or cancelled)."""
        with self.cv:
            self._seen(node)
            drop, deadline = [], self.clock() + self.ttl
            for jid in ids:
                j = self.jobs.get(jid)
                if j is not None and j["node"] == node:
                    j["deadline"] = deadline
                else:
                    drop.append(jid)
            return drop

    def complete(self, node: str, jid: str, status: str, answer: str, tokens_in: int = 0,
                 tokens_out: int = 0) -> bool:
        """The first answer for a task wins (from any node it was given to). -> accepted."""
        with self.cv:
            self._seen(node)
            j = self.jobs.pop(jid, None)
            if j is None:
                return False
            self.nodes[node]["done"] += 1
            j["on_done"](status, answer, node, tokens_in, tokens_out)
            return True

    def close(self):
        """No more tasks: nodes waiting for work are told the swarm is finished."""
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def status(self) -> dict:
        with self.cv:
            now = self.clock()
            running = collections.Counter(j["node"] for j in self.jobs.values() if j["node"])
            return {"pending": sum(1 for j in self.jobs.values() if j["node"] is None),
                    "running": sum(running.values()), "closed": self.closed,
                    "nodes": [{"name": k, "seen_s_ago": round(now - v["seen"], 1), "running": running[k],
                               "done": v["done"], "lost": v["lost"]} for k, v in self.nodes.items()]}


class _QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # a failed TLS handshake or a dropped node
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)


def _str(v, limit: int) -> str:
    if not isinstance(v, str) or len(v) > limit:
        raise ValueError("bad field")
    return v


class HubServer:
    """The HTTP side of a Hub: POST /node/lease, /node/heartbeat, /node/result, GET /node/status,
    each with `Authorization: Bearer <token>`. TLS when certfile is given (required off loopback)."""

    def __init__(self, hub: Hub, token: str, host: str = "127.0.0.1", port: int = 0,
                 certfile: str | None = None, keyfile: str | None = None):
        if not token or len(token) < 16:
            raise ValueError("the node token must be at least 16 characters")
        if not aios_a2a._is_loopback(host) and not certfile:
            raise ValueError(f"listening on {host} needs a TLS certificate (--node-cert / --node-key), so the "
                             "token never crosses the network in plain text; or keep 127.0.0.1 behind an "
                             "https reverse proxy or tunnel")
        self.hub, self.token = hub, token
        self.httpd = _QuietServer((host, port), self._handler())
        self.httpd.daemon_threads = True
        self.tls = bool(certfile)
        if certfile:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(certfile, keyfile)
            # the handshake happens in the request's own thread, so a slow client cannot stall accept()
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True,
                                                do_handshake_on_connect=False)
        self.host, self.port = host, self.httpd.server_address[1]
        self._thread = None
        self._reaper_stop = threading.Event()

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{'https' if self.tls else 'http'}://{host}:{self.port}"

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        threading.Thread(target=self._reaper, daemon=True).start()
        return self

    def _reaper(self):  # tasks of nodes that died are given away even while no node is asking
        while not self._reaper_stop.wait(min(1.0, self.hub.ttl / 4)):
            self.hub.reap()

    def stop(self):
        self._reaper_stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self):
        srv = self

        class Handler(http.server.BaseHTTPRequestHandler):
            timeout = POLL_WAIT + 40  # seconds a connection may idle (a long poll waits POLL_WAIT)
            server_version = "aios-node"
            sys_version = ""

            def log_message(self, *_):
                pass

            def reply(self, code: int, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authed(self) -> bool:
                got = self.headers.get("Authorization", "")
                return hmac.compare_digest(got.encode(), f"Bearer {srv.token}".encode())

            def do_GET(self):
                if self.path != "/node/status":
                    return self.reply(404, {"error": "not found"})
                if not self.authed():
                    return self.reply(401, {"error": "bad token"})
                self.reply(200, srv.hub.status())

            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0 or n > BODY_MAX:
                    aios_a2a.drain_body(self.rfile, n, self.connection)
                    return self.reply(413, {"error": "request too large"})
                raw = self.rfile.read(n)  # read before refusing: Windows resets unread connections
                if not self.authed():
                    return self.reply(401, {"error": "bad token"})
                try:
                    req = json.loads(raw or b"{}")
                    node = _str(req.get("node"), 64)
                    if not NAME_RE.fullmatch(node):
                        raise ValueError("bad node name")
                    if self.path == "/node/lease":
                        wait = min(max(float(req.get("wait", 0)), 0.0), POLL_WAIT)
                        tasks, finished = srv.hub.lease(node, int(req.get("max", 1)), wait)
                        return self.reply(200, {"tasks": tasks, "finished": finished})
                    if self.path == "/node/heartbeat":
                        ids = req.get("ids") or []
                        if not isinstance(ids, list) or len(ids) > 10_000:
                            raise ValueError("bad ids")
                        return self.reply(200, {"drop": srv.hub.heartbeat(node, [_str(i, 64) for i in ids])})
                    if self.path == "/node/result":
                        ok = srv.hub.complete(node, _str(req.get("id"), 64), _str(req.get("status"), 32),
                                              str(req.get("answer", ""))[:ANSWER_MAX],
                                              max(0, int(req.get("tokens_in") or 0)),
                                              max(0, int(req.get("tokens_out") or 0)))
                        return self.reply(200, {"accepted": ok})
                    return self.reply(404, {"error": "not found"})
                except (ValueError, TypeError, AttributeError) as e:
                    return self.reply(400, {"error": f"bad request: {e}"})

        return Handler


def new_token() -> str:
    return secrets.token_urlsafe(24)


def parse_listen(spec: str) -> tuple[str, int]:
    """'8780' -> ('127.0.0.1', 8780); '0.0.0.0:8780', '[::]:8780' -> that address."""
    host, _, port = spec.rpartition(":")
    host = host.strip("[]") or "127.0.0.1"
    if not port.isdigit() or not 0 <= int(port) <= 65535:
        raise ValueError(f"--nodes {spec!r}: give PORT or HOST:PORT")
    return host, int(port)


# ---- the node (worker) side ----

class HeadClient:
    """Talks to a head. Blocking calls (use from a thread)."""

    def __init__(self, url: str, token: str, cafile: str | None = None):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"--head {url!r}: give http(s)://host:port")
        if u.scheme == "http" and not aios_a2a._is_loopback(u.hostname):
            raise ValueError(f"refusing to send the node token over plain http to {u.hostname}: use https")
        if not token:
            raise ValueError("no node token: set $AIOS_NODE_TOKEN (the head prints one)")
        self.base, self.token = f"{u.scheme}://{u.netloc}", token
        self.ctx = ssl.create_default_context(cafile=cafile) if u.scheme == "https" else None

    def _post(self, path: str, body: dict, timeout: float = 30) -> dict:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json", "User-Agent": "aios-node"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
                return json.loads(r.read(BODY_MAX + 1)[:BODY_MAX])
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise PermissionError("the head refused the node token") from None
            raise OSError(f"head answered HTTP {e.code}") from None

    def lease(self, node: str, n: int, wait: float = POLL_WAIT) -> tuple[list[dict], bool]:
        r = self._post("/node/lease", {"node": node, "max": n, "wait": wait}, timeout=wait + 30)
        return list(r.get("tasks") or []), bool(r.get("finished"))

    def heartbeat(self, node: str, ids: list[str]) -> list[str]:
        return list(self._post("/node/heartbeat", {"node": node, "ids": ids}).get("drop") or [])

    def result(self, node: str, jid: str, status: str, answer: str, tokens_in: int = 0, tokens_out: int = 0) -> bool:
        return bool(self._post("/node/result", {"node": node, "id": jid, "status": status[:32],
                                                "answer": answer[:ANSWER_MAX], "tokens_in": tokens_in,
                                                "tokens_out": tokens_out}).get("accepted"))


async def serve_node(client: HeadClient, name: str, parallel: int, run, once: bool = False,
                     heartbeat: float = HEARTBEAT, poll_wait: float = POLL_WAIT, give_up: float = 60.0,
                     log=None) -> dict:
    """Run tasks from the head, `parallel` at a time: `await run(task) -> (status, answer, tokens_in,
    tokens_out)` for each. Retries while the head is unreachable. once: stop when the head says the
    swarm is finished, or after `give_up` seconds without reaching it. -> counts."""
    log = log or (lambda msg: print(f"[aios] node {name}: {msg}", file=sys.stderr))
    running: dict[str, asyncio.Task] = {}
    reporting: set[str] = set()  # finished, answer being sent: not cancelled when the node stops
    slot = asyncio.Event()
    counts = collections.Counter()

    async def call(fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def one(task):
        jid = task["id"]
        try:
            try:
                status, answer, t_in, t_out = await run(task)
            except asyncio.CancelledError:
                counts["dropped"] += 1
                return
            except Exception as e:
                status, answer, t_in, t_out = "error", f"{type(e).__name__}: {e}", 0, 0
            reporting.add(jid)
            for attempt in range(5):  # the head may be busy or restarting
                try:
                    await call(client.result, name, jid, str(status), str(answer), int(t_in), int(t_out))
                    counts[str(status)] += 1
                    break
                except (OSError, ValueError):
                    await asyncio.sleep(min(2 ** attempt, 10))
            else:
                counts["unsent"] += 1  # the head gives the task to another node when the lease runs out
        finally:
            running.pop(jid, None)
            reporting.discard(jid)
            slot.set()

    async def beats():
        while True:
            await asyncio.sleep(heartbeat)
            if running:
                try:
                    for jid in await call(client.heartbeat, name, list(running)):
                        if (t := running.get(jid)) is not None:
                            t.cancel()
                except (OSError, ValueError):
                    pass

    beat = asyncio.create_task(beats())
    backoff, down_since, contacted = 1.0, None, False
    try:
        while True:
            free = parallel - len(running)
            if free <= 0:
                slot.clear()
                await slot.wait()
                continue
            try:
                tasks, finished = await call(client.lease, name, free, poll_wait)
            except PermissionError as e:
                log(str(e))
                counts["refused"] += 1
                break
            except (OSError, ValueError) as e:
                now = time.monotonic()
                down_since = down_since or now
                if once and now - down_since >= give_up:
                    log(f"head unreachable for {give_up:g}s, stopping ({e})")
                    break
                if backoff == 1.0:
                    log(f"cannot reach the head, retrying ({e})")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            if not contacted:
                log(f"connected to {client.base}, running up to {parallel} tasks at once")
            backoff, down_since, contacted = 1.0, None, True
            for task in tasks:
                if isinstance(task, dict) and isinstance(task.get("id"), str):
                    running[task["id"]] = asyncio.create_task(one(task))
            if finished and not tasks:
                if once:
                    break
                await asyncio.sleep(1.0)  # this swarm is over; wait for the head's next one
    finally:
        beat.cancel()
        for jid, t in list(running.items()):
            if jid not in reporting:
                t.cancel()
        await asyncio.gather(beat, *running.values(), return_exceptions=True)
    return dict(counts)
