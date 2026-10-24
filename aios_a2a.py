"""A2A (Agent2Agent protocol) for aios-core: other agents can call one of yours over HTTP, and your agents
can call theirs as tools.

    python aios.py a2a-serve --agent helper --policy policy.example.toml --port 8770   # prints a token
    AIOS_A2A_TOKEN_HELPER=<token> python aios.py chat --a2a helper=http://127.0.0.1:8770 --policy ...

Speaks the core of A2A over JSON-RPC 2.0, in both protocol versions in use: 1.0 (`SendMessage`,
`SendStreamingMessage`, `GetTask`) and 0.3 (`message/send`, `message/stream`, `tasks/get`), plus the agent
card at /.well-known/agent-card.json. The reply is a finished Task with a text artifact; the streaming
methods send the same task as server-sent events (submitted, working, answer, final status). Messages that
carry a `contextId` continue that conversation: the served agent keeps its history for that context
(in memory, bounded). Push notifications and file parts are not supported. Standard library only.

Security model
- Serving: listens on 127.0.0.1, and every JSON-RPC call needs the bearer token (the card is public, as
  A2A expects, and only names the agent). The caller is not trusted: each request runs the served agent
  with its own policy, scopes and budget, starts tainted (no auto_paths / auto_args), and is audited like
  any run. Nobody can approve risky calls. History is kept only within one context (at most
  CONTEXT_TURNS turns, CONTEXTS_KEPT contexts, dropped after CONTEXT_IDLE seconds) and never saved.
- Calling: a remote agent becomes the tool `a2a__<name>`. It must be granted per agent like any tool,
  needs approval like MCP tools (it sends data off the machine), and its answer is tagged untrusted.
  The tool reports the conversation id so the model can continue a multi-turn exchange.
  A token is never sent over plain http to a host other than this machine.
"""
import asyncio
import collections
import hmac
import http.server
import ipaddress
import json
import re
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
CONTEXTS_KEPT = 20  # conversations remembered at once (oldest dropped first)
CONTEXT_TURNS = 20  # requests per conversation
CONTEXT_IDLE = 1800  # seconds before an unused conversation is forgotten
TERMINAL = {"completed", "failed", "canceled", "rejected"}
V1_METHODS = {"SendMessage": "send", "GetTask": "get"}
V03_METHODS = {"message/send": "send", "tasks/get": "get"}
STREAM_METHODS = {"SendStreamingMessage": True, "message/stream": False}  # -> speaks 1.0
UNSUPPORTED_METHODS = {"tasks/resubscribe", "tasks/cancel", "CancelTask", "SubscribeToTask", "ListTasks",
                       "GetExtendedAgentCard"}
NAME_RE = r"[A-Za-z0-9_-]{1,48}"
ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,100}")  # context ids we accept from callers and report to models

# JSON-RPC / A2A error codes
PARSE, INVALID_REQUEST, NO_METHOD, INVALID_PARAMS = -32700, -32600, -32601, -32602
TASK_NOT_FOUND, UNSUPPORTED = -32001, -32004


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
class Server:
    """Runs `make_agent()` for each new conversation (context) and `run(agent, text)` for each message,
    a coroutine on `loop` that returns the answer. Requests are handled one at a time."""

    def __init__(self, name: str, description: str, token: str, run, make_agent, loop, port: int = 8770,
                 timeout: float = 300):
        self.name, self.description, self.token = name, description, token
        self.run_fn, self.make_agent, self.loop, self.timeout = run, make_agent, loop, timeout
        self.tasks: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self.contexts: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self._lock = asyncio.Lock()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.port = self.httpd.server_address[1]

    def card(self) -> dict:
        return {
            "protocolVersion": PROTOCOL, "name": self.name, "description": self.description,
            "url": f"http://127.0.0.1:{self.port}/", "preferredTransport": "JSONRPC", "version": "1",
            "supportedInterfaces": [{"url": f"http://127.0.0.1:{self.port}/", "protocolBinding": "JSONRPC",
                                     "protocolVersion": v} for v in ("1.0", "0.3")],
            "capabilities": {"streaming": True, "pushNotifications": False},
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
        if tid not in (None, ""):  # every task here ends finished: continue in its context instead
            if tid not in self.tasks:
                raise RpcError(TASK_NOT_FOUND, "task not found")
            raise RpcError(UNSUPPORTED, "that task is finished: send a new message with its contextId")
        return text, cid

    def _finish(self, text: str, cid: str, tid: str | None = None) -> dict:
        state, answer = asyncio.run_coroutine_threadsafe(self._execute(text, cid), self.loop).result()
        task = {"id": tid or str(uuid.uuid4()), "contextId": cid, "state": state, "answer": answer,
                "timestamp": now()}
        self.tasks[task["id"]] = task
        while len(self.tasks) > TASKS_KEPT:
            self.tasks.popitem(last=False)
        return task

    def send(self, params, v1: bool) -> dict:
        task = self._finish(*self._accept(params))
        return {"task": self.render(task, True)} if v1 else self.render(task, False)

    def stream(self, params, v1: bool, emit):
        """The streaming send: `emit(result)` for each event, as the SDKs expect them."""
        text, cid = self._accept(params)
        tid = str(uuid.uuid4())

        def status(state, task=None, final=False):
            ev = {"taskId": tid, "contextId": cid,
                  "status": self.render(task, v1)["status"] if task else
                  {"state": "TASK_STATE_" + state.upper() if v1 else state, "timestamp": now()}}
            return {"statusUpdate": ev} if v1 else {"kind": "status-update", "final": final, **ev}

        first = {"id": tid, "contextId": cid, "status": {"state": "TASK_STATE_SUBMITTED" if v1 else "submitted"}}
        emit({"task": first} if v1 else {"kind": "task", **first})
        emit(status("working"))
        task = self._finish(text, cid, tid)
        rendered = self.render(task, v1)
        for art in rendered.get("artifacts", []):
            ev = {"taskId": tid, "contextId": cid, "artifact": art}
            emit({"artifactUpdate": ev} if v1 else
                 {"kind": "artifact-update", "append": False, "lastChunk": True, **ev})
        emit(status(task["state"], task, final=True))

    def get(self, params, v1: bool) -> dict:
        tid = params.get("id") if isinstance(params, dict) else None
        if not isinstance(tid, str) or tid not in self.tasks:
            raise RpcError(TASK_NOT_FOUND, "task not found")
        return self.render(self.tasks[tid], v1)

    @staticmethod
    def render(task: dict, v1: bool) -> dict:
        """A task in the 1.0 JSON shape (enum names, no `kind`) or the 0.3 one."""
        part = {"text": task["answer"]} if v1 else {"kind": "text", "text": task["answer"]}
        out = {"id": task["id"], "contextId": task["contextId"],
               "status": {"state": "TASK_STATE_" + task["state"].upper() if v1 else task["state"],
                          "timestamp": task["timestamp"]}}
        if task["state"] == "completed":
            out["artifacts"] = [{"artifactId": task["id"] + "-answer", "name": "answer", "parts": [part]}]
        else:  # the reason goes in the status message
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

                self.stream(req.get("params"), STREAM_METHODS[method], send_event)
                return None
            if op:
                result = getattr(self, op)(req.get("params"), method in V1_METHODS)
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
                if urllib.parse.urlsplit(self.path).path != "/":
                    return self.reply(404, {"error": "not found"})
                auth = self.headers.get("Authorization", "")
                if not (auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), srv.token.encode())):
                    return self.reply(401, {"error": "bearer token required"}, (("WWW-Authenticate", "Bearer"),))
                try:
                    n = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    return self.reply(411, {"error": "Content-Length required"})
                if not 0 < n <= BODY_MAX:
                    return self.reply(413, {"error": f"body must be 1..{BODY_MAX} bytes"})
                try:
                    req = json.loads(self.rfile.read(n))
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
                raise RuntimeError("A2A task did not finish in time")
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
