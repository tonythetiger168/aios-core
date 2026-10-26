"""A2A (Agent2Agent protocol) for aios-core: other agents can call one of yours over HTTP, and your agents
can call theirs as tools.

    python aios.py a2a-serve --agent helper --policy policy.example.toml --port 8770   # prints a token
    AIOS_A2A_TOKEN_HELPER=<token> python aios.py chat --a2a helper=http://127.0.0.1:8770 --policy ...

Speaks the core of A2A over JSON-RPC 2.0, in both protocol versions in use: 1.0 (`SendMessage`,
`SendStreamingMessage`, `GetTask`, `CancelTask`, `ListTasks`, `SubscribeToTask`) and 0.3 (`message/send`,
`message/stream`, `tasks/get`, `tasks/cancel`, `tasks/resubscribe`), plus the agent card at /.well-known/agent-card.json. The reply is a finished
Task with a text artifact, or, when the caller asks not to wait (1.0 `returnImmediately`, 0.3
`blocking: false`), a working task to poll with GetTask and stop with CancelTask. The streaming methods
send the task as server-sent events (submitted, working, answer, final status); a caller whose stream
broke can subscribe to the unfinished task again and gets the same answer and final status. Messages that carry a
`contextId` continue that conversation: the served agent keeps its history for that context (in memory,
bounded). Canceling a running task stops the agent at its next await and forgets that conversation's
history (it may end mid tool call). Push notifications (1.0 `CreateTaskPushNotificationConfig` and
friends, 0.3 `tasks/pushNotificationConfig/*`, or a config in the send's `configuration`) are off unless
the server is given URL patterns (`push_to`): the finished task is then POSTed to each config's URL
(1.0: a StreamResponse `{"task": ...}`, 0.3: the Task), with its token in `X-A2A-Notification-Token`.
File parts are not supported. Standard library only.

Security model
- Serving: listens on 127.0.0.1, and every JSON-RPC call needs the bearer token (the card is public, as
  A2A expects, and only names the agent). The caller is not trusted: each request runs the served agent
  with its own policy, scopes and budget, starts tainted (no auto_paths / auto_args), and is audited like
  any run. Nobody can approve risky calls. History is kept only within one context (at most
  CONTEXT_TURNS turns, CONTEXTS_KEPT contexts, dropped after CONTEXT_IDLE seconds) and never saved.
  At most TASKS_ACTIVE tasks are unfinished at once; more are rejected.
- Push notifications make this machine send requests to a URL the caller chooses, so only URLs that
  match one of the operator's `push_to` patterns are accepted. A host name must resolve only to public
  addresses (and the POST goes to the address that was checked); a private one is reached only when the
  pattern names it as a literal address or localhost. Redirects are not followed, and a token or
  credentials never go over plain http to a host other than this machine.
- Calling: a remote agent becomes the tool `a2a__<name>`. It must be granted per agent like any tool,
  needs approval like MCP tools (it sends data off the machine), and its answer is tagged untrusted.
  The tool reports the conversation id so the model can continue a multi-turn exchange.
  A token is never sent over plain http to a host other than this machine.
"""
import asyncio
import collections
import fnmatch
import hmac
import http.server
import ipaddress
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

PROTOCOL = "0.3.0"  # the card's top-level version, for 0.3 clients; 1.0 clients read supportedInterfaces
CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")  # 0.3 name, then the older one
BODY_MAX = 64 * 1024  # bytes of one JSON-RPC request
REPLY_MAX = 1024 * 1024  # bytes read from a remote agent
TEXT_MAX = 20_000  # chars kept from a remote agent's answer
TASKS_KEPT = 100  # finished tasks kept for tasks/get
TASKS_ACTIVE = 8  # unfinished tasks at once (they run one at a time; the rest wait)
CONTEXTS_KEPT = 20  # conversations remembered at once (oldest dropped first)
CONTEXT_TURNS = 20  # requests per conversation
CONTEXT_IDLE = 1800  # seconds before an unused conversation is forgotten
TERMINAL = {"completed", "failed", "canceled", "rejected"}
V1_METHODS = {"SendMessage": "send", "GetTask": "get", "CancelTask": "cancel", "ListTasks": "list"}
V03_METHODS = {"message/send": "send", "tasks/get": "get", "tasks/cancel": "cancel"}
STREAM_METHODS = {"SendStreamingMessage": (True, "stream"), "message/stream": (False, "stream"),  # (1.0?, op)
                  "SubscribeToTask": (True, "subscribe"), "tasks/resubscribe": (False, "subscribe")}
UNSUPPORTED_METHODS = {"GetExtendedAgentCard"}
PUSH_METHODS = {"CreateTaskPushNotificationConfig": (True, "set"), "GetTaskPushNotificationConfig": (True, "get"),
                "ListTaskPushNotificationConfigs": (True, "list"), "ListTaskPushNotificationConfig": (True, "list"),
                "DeleteTaskPushNotificationConfig": (True, "delete"),
                "tasks/pushNotificationConfig/set": (False, "set"), "tasks/pushNotificationConfig/get": (False, "get"),
                "tasks/pushNotificationConfig/list": (False, "list"),
                "tasks/pushNotificationConfig/delete": (False, "delete")}
PUSH_PER_TASK = 4  # push configs one task may have
PUSH_TIMEOUT = 10  # seconds one notification may take
CANCELED = "canceled by the caller"
NAME_RE = r"[A-Za-z0-9_-]{1,48}"
ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,100}")  # context ids we accept from callers and report to models

# JSON-RPC / A2A error codes
PARSE, INVALID_REQUEST, NO_METHOD, INVALID_PARAMS = -32700, -32600, -32601, -32602
TASK_NOT_FOUND, NOT_CANCELABLE, UNSUPPORTED = -32001, -32002, -32004


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def state_name(state) -> str:
    """'TASK_STATE_INPUT_REQUIRED' (1.0) or 'input-required' (0.3) -> 'input-required'."""
    s = str(state or "").lower()
    return s.removeprefix("task_state_").replace("_", "-")


def text_of(parts) -> str:
    """Join the text parts of an A2A message or artifact; other kinds are ignored."""
    if not isinstance(parts, list):
        return ""
    return "\n".join(p["text"] for p in parts
                     if isinstance(p, dict) and p.get("kind", "text") == "text" and isinstance(p.get("text"), str))


# ---- serving ----

DRAIN_MAX = 1024 * 1024  # bytes of a refused, oversized request body read and thrown away


def drain_body(rfile, n: int, sock=None, limit: int = DRAIN_MAX):
    """Read and discard up to `limit` bytes of a request body that is refused for its size, so the
    connection closes cleanly. Closing with unread data makes Windows reset it, and the caller sees
    "connection aborted" instead of the 413. Stops early when the caller sends less than it claimed."""
    if sock is not None:
        sock.settimeout(1)  # a caller that sent less than it claimed is not waited on long
    left = min(max(n, 0), limit)
    try:
        while left > 0:
            chunk = rfile.read1(min(left, 65536)) if hasattr(rfile, "read1") else rfile.read(min(left, 65536))
            if not chunk:
                break
            left -= len(chunk)
    except OSError:  # timeout or reset: the refusal is still answered if the socket allows
        pass


class Server:
    """Runs `make_agent()` for each new conversation (context) and `run(agent, text)` for each message,
    a coroutine on `loop` that returns the answer. Requests are handled one at a time."""

    def __init__(self, name: str, description: str, token: str, run, make_agent, loop, port: int = 8770,
                 timeout: float = 300, push_to=()):
        self.name, self.description, self.token = name, description, token
        self.push_to = list(push_to)  # URL glob patterns push notifications may go to; none = no push
        self.run_fn, self.make_agent, self.loop, self.timeout = run, make_agent, loop, timeout
        self.tasks: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self.contexts: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self._lock = asyncio.Lock()  # one run at a time
        self._tasks_lock = threading.Lock()  # self.tasks is used from the HTTP threads and the loop
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.port = self.httpd.server_address[1]

    def card(self) -> dict:
        return {
            "protocolVersion": PROTOCOL, "name": self.name, "description": self.description,
            "url": f"http://127.0.0.1:{self.port}/", "preferredTransport": "JSONRPC", "version": "1",
            "supportedInterfaces": [{"url": f"http://127.0.0.1:{self.port}/", "protocolBinding": "JSONRPC",
                                     "protocolVersion": v} for v in ("1.0", "0.3")],
            "capabilities": {"streaming": True, "pushNotifications": bool(self.push_to)},
            "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
            "skills": [{"id": self.name, "name": self.name, "description": self.description, "tags": ["aios"]}],
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "security": [{"bearer": []}],
        }

    def _context(self, cid: str) -> dict:
        """The conversation `cid`, created if new. Idle and surplus conversations are forgotten."""
        t = time.monotonic()
        for old in [k for k, c in self.contexts.items() if t - c["used"] > CONTEXT_IDLE]:
            del self.contexts[old]
        ctx = self.contexts.pop(cid, None) or {"agent": self.make_agent(), "turns": 0}
        ctx["used"] = t
        self.contexts[cid] = ctx  # most recently used last
        while len(self.contexts) > CONTEXTS_KEPT:
            self.contexts.popitem(last=False)
        return ctx

    async def _execute(self, text: str, cid: str) -> tuple[str, str]:
        async with self._lock:
            ctx = self._context(cid)
            if ctx["turns"] >= CONTEXT_TURNS:
                return "rejected", f"this conversation reached {CONTEXT_TURNS} messages: start a new one"
            ctx["turns"] += 1
            try:
                return "completed", await asyncio.wait_for(self.run_fn(ctx["agent"], text), self.timeout)
            except asyncio.CancelledError:  # canceled mid-run: the history may end in a half-done step
                self.contexts.pop(cid, None)
                raise
            except asyncio.TimeoutError:
                return "failed", f"timed out after {self.timeout:g}s"
            except Exception as e:
                return "failed", f"{type(e).__name__}: {e}"

    def _accept(self, params) -> tuple[str, str]:
        """Validate a send: -> (text, context id)."""
        msg = params.get("message") if isinstance(params, dict) else None
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "ROLE_USER"):
            raise RpcError(INVALID_PARAMS, "params.message must be a message with role 'user'")
        text = text_of(msg.get("parts")).strip()
        if not text:
            raise RpcError(INVALID_PARAMS, "the message needs at least one non-empty text part")
        cid = msg.get("contextId")
        if cid is None or cid == "":
            cid = str(uuid.uuid4())
        elif not (isinstance(cid, str) and ID_RE.fullmatch(cid)):
            raise RpcError(INVALID_PARAMS, "contextId must be 1-100 letters, digits or ._:-")
        tid = msg.get("taskId")
        if tid not in (None, ""):  # no task here waits for input: continue in its context instead
            task = self.tasks.get(tid)
            if not task:
                raise RpcError(TASK_NOT_FOUND, "task not found")
            if task["state"] not in TERMINAL:
                raise RpcError(UNSUPPORTED, "that task is still running: wait for it or cancel it")
            raise RpcError(UNSUPPORTED, "that task is finished: send a new message with its contextId")
        return text, cid

    def _send_push(self, params, v1: bool) -> list[dict]:
        """The push config a send carries in its configuration (validated), as a list of 0 or 1."""
        conf = params.get("configuration") if isinstance(params, dict) else None
        raw = (conf or {}).get("taskPushNotificationConfig" if v1 else "pushNotificationConfig") \
            if isinstance(conf, dict) else None
        return [] if raw is None else [self._push_config(raw, v1)]

    def _push_config(self, raw, v1: bool) -> dict:
        if not self.push_to:
            raise RpcError(UNSUPPORTED, "push notifications are not enabled on this agent")
        if not isinstance(raw, dict) or not isinstance(raw.get("url"), str) or len(raw["url"]) > 2000:
            raise RpcError(INVALID_PARAMS, "a push notification config needs a url")
        url = raw["url"].strip()
        import aios_fetch
        try:
            u = aios_fetch.check_url(url)
        except ValueError as e:
            raise RpcError(INVALID_PARAMS, f"push url: {e}") from None
        if not any(fnmatch.fnmatchcase(url, pat) for pat in self.push_to):
            raise RpcError(INVALID_PARAMS, "this agent does not send push notifications to that url")
        auth = raw.get("authentication") if isinstance(raw.get("authentication"), dict) else {}
        schemes = [auth.get("scheme")] if v1 else (auth.get("schemes") or [None])
        cfg = {"id": raw.get("id") or str(uuid.uuid4()), "url": url, "token": raw.get("token") or "",
               "scheme": schemes[0] or "", "credentials": auth.get("credentials") or "", "v1": v1}
        if not (isinstance(cfg["id"], str) and ID_RE.fullmatch(cfg["id"])):
            raise RpcError(INVALID_PARAMS, "a push config id must be 1-100 letters, digits or ._:-")
        if not all(isinstance(cfg[k], str) and len(cfg[k]) <= 4096 for k in ("token", "scheme", "credentials")):
            raise RpcError(INVALID_PARAMS, "push token, scheme and credentials must be strings (at most 4096)")
        if (cfg["token"] or cfg["credentials"]) and u.scheme == "http" and not _is_loopback(u.hostname):
            raise RpcError(INVALID_PARAMS, "a push token or credentials are only sent over https (or to this machine)")
        return cfg

    @staticmethod
    def _render_push(task: dict, cfg: dict, v1: bool) -> dict:
        """A stored config as the caller sees it (credentials are never sent back)."""
        out = {"id": cfg["id"], "url": cfg["url"]}
        if cfg["token"]:
            out["token"] = cfg["token"]
        if cfg["scheme"]:
            out["authentication"] = {"scheme": cfg["scheme"]} if v1 else {"schemes": [cfg["scheme"]]}
        return {"taskId": task["id"], **out} if v1 else {"taskId": task["id"], "pushNotificationConfig": out}

    def push(self, op: str, params, v1: bool):
        """The push-notification config methods of both versions."""
        if not self.push_to:
            raise RpcError(UNSUPPORTED, "push notifications are not enabled on this agent")
        p = params if isinstance(params, dict) else {}
        task = self._task({"id": p.get("taskId") if v1 or op == "set" else p.get("id")})
        cid = p.get("id") if v1 else p.get("pushNotificationConfigId")
        if op == "set":
            cfg = self._push_config(p if v1 else p.get("pushNotificationConfig"), v1)
            with self._tasks_lock:
                kept = [c for c in task.get("push", []) if c["id"] != cfg["id"]]
                if len(kept) >= PUSH_PER_TASK:
                    raise RpcError(INVALID_PARAMS, f"a task has at most {PUSH_PER_TASK} push configs")
                task["push"] = kept + [cfg]
            return self._render_push(task, cfg, v1)
        with self._tasks_lock:
            configs = list(task.get("push", []))
            if op == "list":
                shown = [self._render_push(task, c, v1) for c in configs]
                return {"configs": shown, "nextPageToken": ""} if v1 else shown
            hit = [c for c in configs if c["id"] == cid] if cid or v1 else configs[:1]
            if not hit:
                raise RpcError(INVALID_PARAMS, "no such push notification config")
            if op == "get":
                return self._render_push(task, hit[0], v1)
            task["push"] = [c for c in configs if c is not hit[0]]
        return {} if v1 else None

    def _notify(self, task: dict) -> None:
        """POST the finished task to each of its push URLs, in the background, once."""
        with self._tasks_lock:
            if task.get("notified") or task["state"] not in TERMINAL:
                return
            task["notified"] = True
            jobs = [(c, {"task": self.render(task, True)} if c["v1"] else self.render(task, False))
                    for c in task.get("push", [])]
        for cfg, body in jobs:
            threading.Thread(target=self._deliver, args=(cfg, body), daemon=True).start()

    def _deliver(self, cfg: dict, body: dict) -> None:
        import aios_fetch
        headers = {"Content-Type": "application/json"}
        if cfg["token"]:
            headers["X-A2A-Notification-Token"] = cfg["token"]
        if cfg["scheme"] and cfg["credentials"]:
            headers["Authorization"] = f"{cfg['scheme']} {cfg['credentials']}"
        host = urllib.parse.urlsplit(cfg["url"]).hostname or ""
        literal = host.lower() == "localhost"
        try:
            literal = literal or bool(ipaddress.ip_address(host))
        except ValueError:
            pass
        try:  # a literal address or localhost was named by the operator's pattern; names must be public
            status = aios_fetch.post_json(cfg["url"], json.dumps(body, ensure_ascii=False).encode(), headers,
                                          PUSH_TIMEOUT, allow_private=literal)
            if not 200 <= status < 300:
                print(f"[a2a] push notification to {cfg['url']}: HTTP {status}", file=sys.stderr)
        except ValueError as e:
            print(f"[a2a] push notification to {cfg['url']} failed: {e}", file=sys.stderr)

    def _settle(self, task: dict, fut) -> None:
        """Record how the run behind `task` ended (once; a cancel already recorded wins)."""
        if fut.cancelled():
            state, answer = "canceled", CANCELED
        else:
            try:
                state, answer = fut.result()
            except BaseException as e:
                state, answer = "failed", f"{type(e).__name__}: {e}"
        with self._tasks_lock:
            if task["state"] not in TERMINAL:
                task.update(state=state, answer=answer, timestamp=now())
            task.pop("future", None)
        self._notify(task)

    def _start(self, text: str, cid: str, tid: str | None = None, push=()) -> dict:
        task = {"id": tid or str(uuid.uuid4()), "contextId": cid, "state": "working", "answer": "",
                "timestamp": now(), "push": list(push)}
        with self._tasks_lock:
            if sum(t["state"] not in TERMINAL for t in self.tasks.values()) >= TASKS_ACTIVE:
                task.update(state="rejected", answer=f"{TASKS_ACTIVE} tasks are already unfinished: try again later")
            else:
                task["future"] = asyncio.run_coroutine_threadsafe(self._execute(text, cid), self.loop)
            self.tasks[task["id"]] = task
            for old in [k for k, t in self.tasks.items() if t["state"] in TERMINAL][:max(0, len(self.tasks) - TASKS_KEPT)]:
                del self.tasks[old]  # oldest finished first; unfinished ones stay reachable
        if "future" in task:
            fut = task["future"]
            fut.add_done_callback(lambda f: self._settle(task, f))
        else:
            self._notify(task)  # rejected at once
        return task

    def _wait(self, task: dict) -> dict:
        fut = task.get("future")
        if fut is not None:
            try:
                fut.result()
            except BaseException:
                pass
            self._settle(task, fut)  # the done callback may not have run yet
        return task

    def send(self, params, v1: bool) -> dict:
        text, cid = self._accept(params)
        push = self._send_push(params, v1)
        conf = params.get("configuration")
        conf = conf if isinstance(conf, dict) else {}
        later = conf.get("returnImmediately") is True if v1 else conf.get("blocking") is False
        task = self._start(text, cid, push=push)
        if not later:
            self._wait(task)
        return {"task": self.render(task, True)} if v1 else self.render(task, False)

    def stream(self, params, v1: bool, emit):
        """The streaming send: `emit(result)` for each event, as the SDKs expect them."""
        text, cid = self._accept(params)
        push = self._send_push(params, v1)
        tid = str(uuid.uuid4())

        first = {"id": tid, "contextId": cid, "status": {"state": "TASK_STATE_SUBMITTED" if v1 else "submitted"}}
        emit({"task": first} if v1 else {"kind": "task", **first})
        ev = {"taskId": tid, "contextId": cid,
              "status": {"state": "TASK_STATE_WORKING" if v1 else "working", "timestamp": now()}}
        emit({"statusUpdate": ev} if v1 else {"kind": "status-update", "final": False, **ev})
        self._follow(self._start(text, cid, tid, push), v1, emit)

    def subscribe(self, params, v1: bool, emit):
        """1.0 SubscribeToTask / 0.3 tasks/resubscribe: the task as it is now, then the rest of its
        events. A finished task has no more events: GetTask returns it."""
        task = self._task(params)
        with self._tasks_lock:
            if task["state"] in TERMINAL:
                raise RpcError(UNSUPPORTED, f"the task is already {task['state']}: use GetTask")
            now_ = self.render(task, v1)
        emit({"task": now_} if v1 else now_)
        self._follow(task, v1, emit)

    def _follow(self, task: dict, v1: bool, emit):
        """Wait for `task`, then emit its answer and final status."""
        self._wait(task)
        tid, cid = task["id"], task["contextId"]
        with self._tasks_lock:
            rendered = self.render(task, v1)
        for art in rendered.get("artifacts", []):
            ev = {"taskId": tid, "contextId": cid, "artifact": art}
            emit({"artifactUpdate": ev} if v1 else
                 {"kind": "artifact-update", "append": False, "lastChunk": True, **ev})
        ev = {"taskId": tid, "contextId": cid, "status": rendered["status"]}
        emit({"statusUpdate": ev} if v1 else {"kind": "status-update", "final": True, **ev})

    def _task(self, params) -> dict:
        tid = params.get("id") if isinstance(params, dict) else None
        task = self.tasks.get(tid) if isinstance(tid, str) else None
        if not task:
            raise RpcError(TASK_NOT_FOUND, "task not found")
        return task

    def get(self, params, v1: bool) -> dict:
        task = self._task(params)
        with self._tasks_lock:
            return self.render(task, v1)

    def cancel(self, params, v1: bool) -> dict:
        task = self._task(params)
        with self._tasks_lock:
            if task["state"] in TERMINAL:
                raise RpcError(NOT_CANCELABLE, f"the task is already {task['state']}")
            task.update(state="canceled", answer=CANCELED, timestamp=now())
            fut = task.get("future")
        if fut is not None:
            fut.cancel()  # stops the run at its next await (or before it starts)
        with self._tasks_lock:
            return self.render(task, v1)

    def list(self, params, v1: bool) -> dict:
        """1.0 ListTasks: newest first, one page (filters: contextId, status)."""
        p = params if isinstance(params, dict) else {}
        size = p.get("pageSize") or 50
        if not isinstance(size, int) or not 1 <= size <= 100:
            raise RpcError(INVALID_PARAMS, "pageSize must be 1..100")
        if p.get("pageToken"):
            raise RpcError(INVALID_PARAMS, "there is only one page")
        cid, want = p.get("contextId"), state_name(p.get("status")) if p.get("status") else None
        with self._tasks_lock:
            hits = [t for t in reversed(self.tasks.values())
                    if (not cid or t["contextId"] == cid) and (not want or t["state"] == want)]
            page = [self.render(t, True) for t in hits[:size]]
        return {"tasks": page, "nextPageToken": "", "pageSize": size, "totalSize": len(hits)}

    @staticmethod
    def render(task: dict, v1: bool) -> dict:
        """A task in the 1.0 JSON shape (enum names, no `kind`) or the 0.3 one."""
        part = {"text": task["answer"]} if v1 else {"kind": "text", "text": task["answer"]}
        out = {"id": task["id"], "contextId": task["contextId"],
               "status": {"state": "TASK_STATE_" + task["state"].upper() if v1 else task["state"],
                          "timestamp": task["timestamp"]}}
        if task["state"] == "completed":
            out["artifacts"] = [{"artifactId": task["id"] + "-answer", "name": "answer", "parts": [part]}]
        elif task["answer"]:  # the reason goes in the status message
            msg = {"messageId": task["id"] + "-status", "role": "ROLE_AGENT" if v1 else "agent", "parts": [part]}
            out["status"]["message"] = msg if v1 else {"kind": "message", **msg}
        return out if v1 else {"kind": "task", **out}

    def dispatch(self, req, start_stream=None) -> dict | None:
        """One JSON-RPC request -> its response. A streaming method calls `start_stream()` for an
        `emit(response)` and returns None once the stream is complete (errors before the first event
        are ordinary responses)."""
        rid = req.get("id") if isinstance(req, dict) else None
        try:
            if not (isinstance(req, dict) and req.get("jsonrpc") == "2.0" and isinstance(req.get("method"), str)):
                raise RpcError(INVALID_REQUEST, "not a JSON-RPC 2.0 request")
            method = req["method"]
            op = V1_METHODS.get(method) or V03_METHODS.get(method)
            if method in STREAM_METHODS and start_stream:
                emit = None

                def send_event(result):
                    nonlocal emit
                    emit = emit or start_stream()
                    emit({"jsonrpc": "2.0", "id": rid, "result": result})

                v1, sop = STREAM_METHODS[method]
                getattr(self, sop)(req.get("params"), v1, send_event)
                return None
            if op:
                result = getattr(self, op)(req.get("params"), method in V1_METHODS)
            elif method in PUSH_METHODS:
                v1, pop = PUSH_METHODS[method]
                result = self.push(pop, req.get("params"), v1)
            elif method in UNSUPPORTED_METHODS or method in STREAM_METHODS or "PushNotificationConfig" in method or method.startswith(
                    "tasks/pushNotificationConfig/"):
                raise RpcError(UNSUPPORTED, f"{method} is not supported by this agent")
            else:
                raise RpcError(NO_METHOD, f"unknown method {method[:60]!r}")
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except RpcError as e:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": str(e)}}

    def _handler(self):
        srv = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version, sys_version = "aios-a2a", ""

            def log_message(self, *a):
                pass

            def reply(self, code: int, body: dict, extra=()):
                data = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(code)
                for k, v in (("Content-Type", "application/json"), ("Content-Length", str(len(data))), *extra):
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def do_GET(self):
                if urllib.parse.urlsplit(self.path).path in CARD_PATHS:
                    return self.reply(200, srv.card())
                self.reply(404, {"error": "not found"})

            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self.close_connection = True
                    return self.reply(411, {"error": "Content-Length required"})
                if not 0 < n <= BODY_MAX:
                    self.close_connection = True
                    drain_body(self.rfile, n, self.connection)
                    return self.reply(413, {"error": f"body must be 1..{BODY_MAX} bytes"})
                # read the body before any refusal: closing with it unread resets the connection on
                # Windows, and the caller sees "connection aborted" instead of our answer
                body = self.rfile.read(n)
                if urllib.parse.urlsplit(self.path).path != "/":
                    return self.reply(404, {"error": "not found"})
                auth = self.headers.get("Authorization", "")
                if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), srv.token.encode())):
                    return self.reply(401, {"error": "bearer token required"}, (("WWW-Authenticate", "Bearer"),))
                try:
                    req = json.loads(body)
                except ValueError:
                    return self.reply(200, {"jsonrpc": "2.0", "id": None,
                                            "error": {"code": PARSE, "message": "invalid JSON"}})
                streaming = []

                def start_stream():
                    self.send_response(200)
                    for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-cache"),
                                 ("Connection", "close")):
                        self.send_header(k, v)
                    self.end_headers()
                    self.close_connection = True
                    streaming.append(True)

                    def emit(body):
                        self.wfile.write(b"data: " + json.dumps(body, ensure_ascii=False).encode() + b"\n\n")
                        self.wfile.flush()
                    return emit

                try:
                    out = srv.dispatch(req, start_stream)
                except OSError:  # the caller went away mid-stream
                    return
                if out is not None:
                    self.reply(200, out)

            def other(self):
                self.reply(405, {"error": "method not allowed"})

            do_PUT = do_DELETE = do_PATCH = do_OPTIONS = other

        return Handler

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# ---- calling ----
def _is_loopback(host: str | None) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


class Client:
    """A remote A2A agent. Blocking calls (use from a thread)."""

    def __init__(self, url: str, token: str | None = None, timeout: float = 300):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"A2A url must be http(s)://host[:port]/..., not {url!r}")
        if token and u.scheme == "http" and not _is_loopback(u.hostname):
            raise ValueError(f"refusing to send a token over plain http to {u.hostname}: use https")
        self.url, self.token, self.timeout = url, token, timeout
        self.base = f"{u.scheme}://{u.netloc}"
        self.v1 = False  # set by card(): speak 1.0 when the agent offers it

    def _open(self, req) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read(REPLY_MAX + 1)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"A2A agent answered HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"cannot reach A2A agent at {self.base}: {getattr(e, 'reason', e)}") from None
        if len(data) > REPLY_MAX:
            raise RuntimeError("A2A agent answer is too large")
        try:
            return json.loads(data)
        except ValueError:
            raise RuntimeError("A2A agent did not answer with JSON") from None

    def card(self) -> dict:
        last = None
        for path in CARD_PATHS:
            try:
                card = self._open(urllib.request.Request(self.base + path, headers={"Accept": "application/json"}))
                if isinstance(card, dict) and isinstance(card.get("name"), str):
                    ifaces = card.get("supportedInterfaces") if isinstance(card.get("supportedInterfaces"), list) else []
                    self.v1 = any(isinstance(i, dict) and str(i.get("protocolVersion", "")).startswith("1")
                                  and i.get("protocolBinding", "JSONRPC") == "JSONRPC" for i in ifaces)
                    return card  # its URLs are ignored: we post to the URL we were given (our token goes there)
            except RuntimeError as e:
                last = e
        raise RuntimeError(f"no A2A agent card at {self.base} ({last or 'not a card'})")

    def _rpc(self, method: str, params: dict) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "A2A-Version": "1.0" if self.v1 else "0.3"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        out = self._open(urllib.request.Request(self.url, data=body, headers=headers, method="POST"))
        if not isinstance(out, dict):
            raise RuntimeError("A2A agent sent a malformed answer")
        if "error" in out:
            err = out["error"] if isinstance(out["error"], dict) else {}
            raise RuntimeError(f"A2A error {err.get('code')}: {str(err.get('message'))[:200]}")
        if not isinstance(out.get("result"), dict):
            raise RuntimeError("A2A agent sent no result")
        return out["result"]

    def send(self, text: str, poll: float = 1.0) -> str:
        r = self.ask(text, poll=poll)
        return r["answer"] if r["state"] == "completed" else f"(the remote agent needs more: {r['state']})\n{r['answer']}"

    def ask(self, text: str, context_id: str | None = None, task_id: str | None = None,
            poll: float = 1.0) -> dict:
        """Send one message, optionally continuing a conversation (and a task that asked for more).
        -> {"answer", "state", "context_id", "task_id"}; ids are None when the agent's are unusable."""
        ids = {k: v for k, v in (("contextId", context_id), ("taskId", task_id)) if v}
        if self.v1:
            msg = {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": text}], **ids}
            res = self._rpc("SendMessage", {"message": msg, "configuration": {"acceptedOutputModes": ["text/plain"]}})
            if isinstance(res.get("message"), dict):
                return self._result(text_of(res["message"].get("parts")), "completed", res["message"])
            res = res.get("task") if isinstance(res.get("task"), dict) else {}
        else:
            msg = {"kind": "message", "role": "user", "messageId": str(uuid.uuid4()),
                   "parts": [{"kind": "text", "text": text}], **ids}
            res = self._rpc("message/send", {"message": msg, "configuration": {
                "blocking": True, "acceptedOutputModes": ["text/plain"]}})
            if res.get("kind") == "message":
                return self._result(text_of(res.get("parts")), "completed", res)
        deadline = time.monotonic() + self.timeout
        while state_name((res.get("status") or {}).get("state")) not in TERMINAL | {"input-required", "auth-required"}:
            if time.monotonic() > deadline:
                try:  # don't leave it running on their side
                    self._rpc("CancelTask" if self.v1 else "tasks/cancel", {"id": res.get("id")})
                except RuntimeError:
                    pass
                raise RuntimeError("A2A task did not finish in time (asked the agent to cancel it)")
            time.sleep(poll)
            res = self._rpc("GetTask" if self.v1 else "tasks/get", {"id": res.get("id")})
        status = res.get("status") or {}
        answer = "\n".join(filter(None, [text_of(a.get("parts")) for a in res.get("artifacts") or []
                                         if isinstance(a, dict)] + [text_of((status.get("message") or {}).get("parts"))]))
        state = state_name(status.get("state"))
        if state in ("failed", "canceled", "rejected"):
            raise RuntimeError(f"A2A task {state}: {answer[:500]}")
        return self._result(answer, state, res, task=state != "completed")

    @staticmethod
    def _result(answer: str, state: str, obj: dict, task: bool = False) -> dict:
        def clean(v):
            return v if isinstance(v, str) and ID_RE.fullmatch(v) else None
        return {"answer": answer[:TEXT_MAX], "state": state, "context_id": clean(obj.get("contextId")),
                "task_id": clean(obj.get("id")) if task else None}
