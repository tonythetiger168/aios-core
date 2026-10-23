"""A2A (Agent2Agent protocol) for aios-core: other agents can call one of yours over HTTP, and your agents
can call theirs as tools.

    python aios.py a2a-serve --agent helper --policy policy.example.toml --port 8770   # prints a token
    AIOS_A2A_TOKEN_HELPER=<token> python aios.py chat --a2a helper=http://127.0.0.1:8770 --policy ...

Speaks the core of A2A over JSON-RPC 2.0, in both protocol versions in use: 1.0 (`SendMessage`, `GetTask`)
and 0.3 (`message/send`, `tasks/get`), plus the agent card at /.well-known/agent-card.json. The reply is
a finished Task with a text artifact. Streaming, push notifications and file parts are not supported.
Standard library only.

Security model
- Serving: listens on 127.0.0.1, and every JSON-RPC call needs the bearer token (the card is public, as
  A2A expects, and only names the agent). The caller is not trusted: each request runs the served agent
  with its own policy, scopes and budget, starts tainted (no auto_paths / auto_args), keeps no history
  between requests, and is audited like any run. Nobody can approve risky calls.
- Calling: a remote agent becomes the tool `a2a__<name>`. It must be granted per agent like any tool,
  needs approval like MCP tools (it sends data off the machine), and its answer is tagged untrusted.
  A token is never sent over plain http to a host other than this machine.
"""
import asyncio
import collections
import hmac
import http.server
import ipaddress
import json
import threading
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
TERMINAL = {"completed", "failed", "canceled", "rejected"}
V1_METHODS = {"SendMessage": "send", "GetTask": "get"}
V03_METHODS = {"message/send": "send", "tasks/get": "get"}
UNSUPPORTED_METHODS = {"message/stream", "tasks/resubscribe", "tasks/cancel", "SendStreamingMessage", "CancelTask",
                       "SubscribeToTask", "ListTasks", "GetExtendedAgentCard"}
NAME_RE = r"[A-Za-z0-9_-]{1,48}"

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
    """Runs `make_agent()` for each `message/send`. `run(agent, text)` is a coroutine on `loop` that
    returns the answer; requests are handled one at a time."""

    def __init__(self, name: str, description: str, token: str, run, make_agent, loop, port: int = 8770,
                 timeout: float = 300):
        self.name, self.description, self.token = name, description, token
        self.run_fn, self.make_agent, self.loop, self.timeout = run, make_agent, loop, timeout
        self.tasks: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self._lock = asyncio.Lock()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.port = self.httpd.server_address[1]

    def card(self) -> dict:
        return {
            "protocolVersion": PROTOCOL, "name": self.name, "description": self.description,
            "url": f"http://127.0.0.1:{self.port}/", "preferredTransport": "JSONRPC", "version": "1",
            "supportedInterfaces": [{"url": f"http://127.0.0.1:{self.port}/", "protocolBinding": "JSONRPC",
                                     "protocolVersion": v} for v in ("1.0", "0.3")],
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
            "skills": [{"id": self.name, "name": self.name, "description": self.description, "tags": ["aios"]}],
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "security": [{"bearer": []}],
        }

    async def _execute(self, text: str) -> tuple[str, str]:
        async with self._lock:
            agent = self.make_agent()
            try:
                return "completed", await asyncio.wait_for(self.run_fn(agent, text), self.timeout)
            except asyncio.TimeoutError:
                return "failed", f"timed out after {self.timeout:g}s"
            except Exception as e:
                return "failed", f"{type(e).__name__}: {e}"

    def send(self, params, v1: bool) -> dict:
        msg = params.get("message") if isinstance(params, dict) else None
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "ROLE_USER"):
            raise RpcError(INVALID_PARAMS, "params.message must be a message with role 'user'")
        text = text_of(msg.get("parts")).strip()
        if not text:
            raise RpcError(INVALID_PARAMS, "the message needs at least one non-empty text part")
        context = msg.get("contextId") if isinstance(msg.get("contextId"), str) else str(uuid.uuid4())
        state, answer = asyncio.run_coroutine_threadsafe(self._execute(text), self.loop).result()
        task = {"id": str(uuid.uuid4()), "contextId": context[:100], "state": state, "answer": answer,
                "timestamp": now()}
        self.tasks[task["id"]] = task
        while len(self.tasks) > TASKS_KEPT:
            self.tasks.popitem(last=False)
        return {"task": self.render(task, True)} if v1 else self.render(task, False)

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

    def dispatch(self, req) -> dict:
        rid = req.get("id") if isinstance(req, dict) else None
        try:
            if not (isinstance(req, dict) and req.get("jsonrpc") == "2.0" and isinstance(req.get("method"), str)):
                raise RpcError(INVALID_REQUEST, "not a JSON-RPC 2.0 request")
            method = req["method"]
            op = V1_METHODS.get(method) or V03_METHODS.get(method)
            if op:
                result = getattr(self, op)(req.get("params"), method in V1_METHODS)
            elif method in UNSUPPORTED_METHODS or "PushNotificationConfig" in method or method.startswith(
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
                self.reply(200, srv.dispatch(req))

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
        if self.v1:
            msg = {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": text}]}
            res = self._rpc("SendMessage", {"message": msg, "configuration": {"acceptedOutputModes": ["text/plain"]}})
            if isinstance(res.get("message"), dict):
                return text_of(res["message"].get("parts"))[:TEXT_MAX]
            res = res.get("task") if isinstance(res.get("task"), dict) else {}
        else:
            msg = {"kind": "message", "role": "user", "messageId": str(uuid.uuid4()),
                   "parts": [{"kind": "text", "text": text}]}
            res = self._rpc("message/send", {"message": msg, "configuration": {
                "blocking": True, "acceptedOutputModes": ["text/plain"]}})
            if res.get("kind") == "message":
                return text_of(res.get("parts"))[:TEXT_MAX]
        import time
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
        if state != "completed":
            answer = f"(the remote agent needs more: {state})\n{answer}"
        return answer[:TEXT_MAX]
