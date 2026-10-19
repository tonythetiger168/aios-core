"""Minimal AIOS kernel (Claude-powered): agents + tools + persistent memory + scheduler + permissions.

pip install anthropic
export ANTHROPIC_API_KEY=...
"""
import argparse
import asyncio
import importlib.util
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from anthropic import AsyncAnthropic

try:
    import resource  # Unix only
except ImportError:  # Windows
    resource = None

IS_WINDOWS = sys.platform == "win32"

MODEL = "claude-sonnet-5-5"
WORKSPACE = Path("./workspace").resolve()
DB_PATH = "aios.db"
HISTORY_LIMIT = 40  # messages reloaded per agent
MCP_MAX_RESULT = 20_000  # chars kept from one MCP tool result
FILE_MAX_CHARS = 50_000  # max chars read/written by file tools
LIST_MAX = 200  # max entries returned by list_files


# ---- Persistent memory (SQLite) ----
class Store:
    def __init__(self, path: str = DB_PATH):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS facts(
                agent TEXT, key TEXT, value TEXT, PRIMARY KEY(agent, key));
        """)

    def add_message(self, agent: str, msg: dict):
        self.db.execute("INSERT INTO messages(agent, body) VALUES(?,?)",
                        (agent, json.dumps(msg, ensure_ascii=False)))
        self.db.commit()

    def load_messages(self, agent: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT body FROM messages WHERE agent=? ORDER BY id DESC LIMIT ?",
            (agent, HISTORY_LIMIT)).fetchall()
        msgs = [json.loads(r[0]) for r in reversed(rows)]
        # history must start at a plain user turn (no orphaned tool_result)
        while msgs and not (msgs[0]["role"] == "user" and isinstance(msgs[0]["content"], str)):
            msgs.pop(0)
        return msgs

    def replace_messages(self, agent: str, msgs: list[dict]):
        with self.db:  # one transaction
            self.db.execute("DELETE FROM messages WHERE agent=?", (agent,))
            self.db.executemany("INSERT INTO messages(agent, body) VALUES(?,?)",
                                [(agent, json.dumps(m, ensure_ascii=False)) for m in msgs])

    def clear_messages(self, agent: str):
        self.db.execute("DELETE FROM messages WHERE agent=?", (agent,))
        self.db.commit()

    def set_fact(self, agent: str, key: str, value: str):
        self.db.execute("INSERT OR REPLACE INTO facts VALUES(?,?,?)", (agent, key, value))
        self.db.commit()

    def get_fact(self, agent: str, key: str) -> str | None:
        r = self.db.execute("SELECT value FROM facts WHERE agent=? AND key=?",
                            (agent, key)).fetchone()
        return r[0] if r else None

    def all_facts(self, agent: str) -> dict[str, str]:
        return dict(self.db.execute(
            "SELECT key, value FROM facts WHERE agent=?", (agent,)).fetchall())


# ---- Tool registry (like syscalls) ----
class ToolRegistry:
    def __init__(self):
        self._fns, self._schemas, self._ctx = {}, {}, set()

    def register(self, name: str, description: str, properties: dict,
                 required=(), ctx: bool = False):
        """ctx=True: the kernel passes `agent=<Agent>` to the tool."""
        def deco(fn):
            self._fns[name] = fn
            if ctx:
                self._ctx.add(name)
            self._schemas[name] = {
                "name": name,
                "description": description,
                "input_schema": {"type": "object", "properties": properties,
                                 "required": list(required)},
            }
            return fn
        return deco

    def register_raw(self, name: str, description: str, input_schema: dict, fn):
        """Register a tool whose JSON schema already exists (e.g. from an MCP server)."""
        self._fns[name] = fn
        self._schemas[name] = {"name": name, "description": description,
                               "input_schema": input_schema}

    @staticmethod
    def permits(perms: set[str], name: str) -> bool:
        """Exact match, or a 'prefix*' wildcard such as 'demo__*'."""
        return name in perms or any(p.endswith("*") and name.startswith(p[:-1]) for p in perms)

    def schemas_for(self, perms: set[str]) -> list[dict]:
        return [s for n, s in self._schemas.items() if self.permits(perms, n)]

    async def call(self, name: str, agent: "Agent", **kwargs):
        if name in self._ctx:
            kwargs["agent"] = agent
        return await self._fns[name](**kwargs)


# ---- Agent (process) ----
@dataclass
class Agent:
    name: str
    permissions: set[str]
    system: str = "You are an agent running inside AIOS. Use tools when needed."
    history: list = field(default_factory=list)
    loaded: bool = False


# ---- Kernel ----
class Kernel:
    def __init__(self, store: Store | None = None):
        self.client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY
        self.tools = ToolRegistry()
        self.store = store or Store()
        self.queue: asyncio.Queue = asyncio.Queue()
        self._stack = AsyncExitStack()  # owns MCP server connections

    async def connect_mcp(self, server: str, command: str, args=(), env=None) -> list[str]:
        """Start an MCP server (stdio) and register its tools as '<server>__<tool>'.
        Agents must be granted them explicitly, e.g. permissions={'<server>__*'}."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=command, args=list(args), env=env)
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        def make(tool_name: str):
            async def call(**kwargs):
                res = await session.call_tool(tool_name, kwargs)
                text = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
                if res.isError:
                    raise RuntimeError(text or "MCP tool error")
                return text[:MCP_MAX_RESULT]
            return call

        names = []
        for t in (await session.list_tools()).tools:
            full = f"{server}__{t.name}"
            self.tools.register_raw(full, t.description or "", t.inputSchema, make(t.name))
            names.append(full)
        return names

    async def close(self):
        await self._stack.aclose()

    async def submit(self, agent: Agent, task: str):
        await self.queue.put((agent, task))

    def _push(self, agent: Agent, msg: dict):
        agent.history.append(msg)
        self.store.add_message(agent.name, msg)

    def _system(self, agent: Agent) -> str:
        facts = self.store.all_facts(agent.name)
        if not facts:
            return agent.system
        lines = "\n".join(f"- {k}: {v}" for k, v in facts.items())
        return f"{agent.system}\n\nLong-term memory:\n{lines}"

    @staticmethod
    def _clean(history: list[dict]) -> list[dict]:
        """Make stored history safe to resend.
        - Drop thinking blocks from finished turns: their signatures are bound to the
          system prompt they were made under, which changes when long-term memory does.
        - Drop an unfinished tail (e.g. a run that crashed mid tool-loop)."""
        out = []
        for m in history:
            c = m["content"]
            if m["role"] == "assistant" and isinstance(c, list):
                c = [b for b in c if b.get("type") not in ("thinking", "redacted_thinking")]
                if not c:
                    continue
                m = {**m, "content": c}
            out.append(m)

        def finished(m):
            return m["role"] == "assistant" and not any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in m["content"])

        while out and not finished(out[-1]):
            out.pop()
        return out

    async def _run(self, agent: Agent, task: str, max_steps: int = 8) -> str:
        if not agent.loaded:  # restore history after restart
            agent.history = self.store.load_messages(agent.name)
            agent.loaded = True
        cleaned = self._clean(agent.history)
        if cleaned != agent.history:
            agent.history = cleaned
            self.store.replace_messages(agent.name, cleaned)
        system = self._system(agent)  # frozen for the whole run (thinking blocks bind to it)
        self._push(agent, {"role": "user", "content": task})

        for _ in range(max_steps):
            resp = await self.client.messages.create(
                model=MODEL, max_tokens=1024,
                system=system,
                tools=self.tools.schemas_for(agent.permissions),
                messages=agent.history,
            )
            self._push(agent, {"role": "assistant",
                               "content": [b.model_dump(exclude_none=True) for b in resp.content]})
            if resp.stop_reason != "tool_use":
                return "".join(b.text for b in resp.content if b.type == "text")

            results = []
            for b in resp.content:
                if b.type != "tool_use":
                    continue
                if not ToolRegistry.permits(agent.permissions, b.name):  # permission check
                    out, err = f"permission denied: {b.name}", True
                else:
                    try:
                        out, err = await self.tools.call(b.name, agent, **b.input), False
                    except Exception as e:
                        out, err = f"{type(e).__name__}: {e}", True
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": str(out), "is_error": err})
            self._push(agent, {"role": "user", "content": results})
        return "(max steps reached)"

    async def run_forever(self, workers: int = 4):
        async def worker():
            while True:
                agent, task = await self.queue.get()
                try:
                    print(f"[{agent.name}]", await self._run(agent, task))
                except Exception as e:
                    print(f"[{agent.name}] error: {e}")
                finally:
                    self.queue.task_done()
        await asyncio.gather(*(worker() for _ in range(workers)))


# ---- Sandbox: untrusted code runs in an isolated, resource-limited subprocess ----
class Sandbox:
    def __init__(self, timeout=10, cpu_s=5, mem_mb=256, max_out=10_000):
        self.timeout, self.cpu_s, self.mem = timeout, cpu_s, mem_mb * 1024 * 1024
        self.max_out = max_out
        # network isolation via a fresh network namespace (Linux, needs unprivileged userns)
        self.net_isolated = (not IS_WINDOWS and bool(shutil.which("unshare"))
                             and subprocess.run(["unshare", "-rn", "true"],
                                                capture_output=True).returncode == 0)
        if IS_WINDOWS:
            print("[sandbox] WARNING: Windows = timeout only (no memory/network limits). "
                  "Use WSL2 or Docker for real isolation.")
        elif not self.net_isolated:
            print("[sandbox] WARNING: network isolation unavailable on this host")

    def _limits(self):
        cpu, mem = self.cpu_s, self.mem
        def apply():
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            resource.setrlimit(resource.RLIMIT_FSIZE, (10 << 20, 10 << 20))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        return apply

    async def _kill(self, proc):
        if IS_WINDOWS:  # kill the whole process tree
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)  # kill the whole process group
        await proc.wait()

    async def run_python(self, code: str) -> str:
        WORKSPACE.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=WORKSPACE) as d:  # throwaway cwd
            Path(d, "main.py").write_text(code, encoding="utf-8")
            cmd = [sys.executable, "-I", "main.py"]  # -I: ignore env vars and user site
            if self.net_isolated:
                cmd = ["unshare", "-rn"] + cmd
            if IS_WINDOWS:
                env = {"SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}  # Python needs this
                extra = {}
            else:
                env = {"PATH": "/usr/bin:/bin"}
                extra = {"preexec_fn": self._limits(), "start_new_session": True}
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=d, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL, **extra)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
            except asyncio.TimeoutError:
                await self._kill(proc)
                return f"killed: timeout after {self.timeout}s"
            text = out.decode(errors="replace")
            if len(text) > self.max_out:
                text = text[: self.max_out] + "\n...(truncated)"
            return f"exit={proc.returncode}\n{text}"


# ---- Built-in tools ----
def install_builtin_tools(k: Kernel):
    # Windows has no real sandbox here, so run_python is opt-in there.
    if not IS_WINDOWS or os.environ.get("AIOS_ALLOW_WEAK_SANDBOX") == "1":
        sandbox = Sandbox()

        @k.tools.register("run_python", "Run Python code in an isolated sandbox. "
                          "10s timeout. Print results to stdout.",
                          {"code": {"type": "string"}}, required=["code"])
        async def run_python(code: str):
            return await sandbox.run_python(code)
    else:
        print("[aios] run_python disabled on Windows (no real sandbox). "
              "Use WSL2/Docker, or set AIOS_ALLOW_WEAK_SANDBOX=1 to enable it unsafely.")
    @k.tools.register("now", "Get the current local date and time.", {})
    async def now():
        return datetime.now().isoformat(timespec="seconds")

    def resolve(path: str, allow_root: bool = False) -> Path:
        """Map a relative path into the workspace; refuse anything that escapes it."""
        p = (WORKSPACE / path).resolve()
        if WORKSPACE not in p.parents and not (allow_root and p == WORKSPACE):
            raise PermissionError("path escapes workspace")
        return p

    @k.tools.register("read_file", "Read a text file from the agent workspace.",
                      {"path": {"type": "string", "description": "relative path"}},
                      required=["path"])
    async def read_file(path: str):
        text = resolve(path).read_text(encoding="utf-8", errors="replace")
        return text[:FILE_MAX_CHARS] + ("\n...(truncated)" if len(text) > FILE_MAX_CHARS else "")

    @k.tools.register("write_file", "Create or overwrite a text file in the agent workspace "
                      "(parent folders are created).",
                      {"path": {"type": "string", "description": "relative path"},
                       "content": {"type": "string"}},
                      required=["path", "content"])
    async def write_file(path: str, content: str):
        if len(content) > FILE_MAX_CHARS:
            raise ValueError(f"content too large (max {FILE_MAX_CHARS} chars)")
        p = resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(content, encoding="utf-8")
        return f"{'overwrote' if existed else 'created'} {p.relative_to(WORKSPACE).as_posix()}"

    @k.tools.register("list_files", "List files in the agent workspace, optionally under a subfolder.",
                      {"folder": {"type": "string", "description": "relative folder, default: root"}})
    async def list_files(folder: str = ""):
        base = resolve(folder, allow_root=True)
        if not base.is_dir():
            raise NotADirectoryError(folder or ".")
        items = sorted(q for q in base.rglob("*")
                       if q.is_file() and not q.relative_to(WORKSPACE).parts[0].startswith("tmp"))
        lines = [f"{q.relative_to(WORKSPACE).as_posix()}  ({q.stat().st_size} bytes)"
                 for q in items[:LIST_MAX]]
        if len(items) > LIST_MAX:
            lines.append(f"...({len(items) - LIST_MAX} more)")
        return "\n".join(lines) or "(empty)"

    @k.tools.register("remember", "Save a fact to long-term memory (persists across restarts).",
                      {"key": {"type": "string"}, "value": {"type": "string"}},
                      required=["key", "value"], ctx=True)
    async def remember(key: str, value: str, agent: Agent):
        k.store.set_fact(agent.name, key, value)
        return "saved"

    @k.tools.register("recall", "Read a fact from long-term memory.",
                      {"key": {"type": "string"}}, required=["key"], ctx=True)
    async def recall(key: str, agent: Agent):
        return k.store.get_fact(agent.name, key) or "not found"

    @k.tools.register("list_facts", "List every key in long-term memory.", {}, ctx=True)
    async def list_facts(agent: Agent):
        facts = k.store.all_facts(agent.name)
        return "\n".join(f"{key}: {val}" for key, val in facts.items()) or "(no facts yet)"


DEFAULT_TOOLS = ("now", "read_file", "write_file", "list_files", "remember", "recall", "list_facts", "run_python")


# ---- Demo ----
async def demo():
    WORKSPACE.mkdir(exist_ok=True)
    (WORKSPACE / "note.txt").write_text("AIOS test note: agents are processes.", encoding="utf-8")

    k = Kernel()
    install_builtin_tools(k)

    perms = set(DEFAULT_TOOLS)
    task = "Read note.txt, remember its content under key 'note', then tell me the time."
    if importlib.util.find_spec("mcp"):  # optional: pip install mcp
        print("MCP tools:", await k.connect_mcp("demo", sys.executable, ["demo_mcp_server.py"]))
        perms.add("demo__*")
        task += " Also use the demo tools to compute 17+25."

    agent = Agent("a1", perms)
    await k.submit(agent, task)
    runner = asyncio.create_task(k.run_forever())
    await k.queue.join()
    runner.cancel()
    await k.close()


# ---- Interactive CLI ----
async def chat(agent_name: str, mcp_specs: list[str]):
    WORKSPACE.mkdir(exist_ok=True)
    k = Kernel()
    install_builtin_tools(k)
    perms = set(DEFAULT_TOOLS)
    for spec in mcp_specs:  # "name=command arg1 arg2"
        name, _, cmdline = spec.partition("=")
        cmd, *args = shlex.split(cmdline)
        print(f"MCP {name}:", await k.connect_mcp(name, cmd, args))
        perms.add(f"{name}__*")

    agent = Agent(agent_name, perms)
    print(f"AIOS agent '{agent_name}' ready.  /facts  /reset  /quit")
    try:
        while True:
            try:
                line = (await asyncio.to_thread(input, "you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line == "/quit":
                break
            if line == "/facts":
                print(k.store.all_facts(agent.name) or "(no facts yet)")
            elif line == "/reset":
                k.store.clear_messages(agent.name)
                agent.history, agent.loaded = [], True
                print("history cleared (long-term facts kept)")
            else:
                try:
                    print(await k._run(agent, line))
                except Exception as e:
                    print(f"error: {type(e).__name__}: {e}")
    finally:
        await k.close()


def cli():
    ap = argparse.ArgumentParser(prog="aios", description="Minimal AIOS")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("chat", help="interactive agent session")
    c.add_argument("--agent", default="main", help="agent name (own memory per name)")
    c.add_argument("--mcp", action="append", default=[], metavar="NAME=CMD ARGS",
                   help='attach an MCP server, e.g. --mcp "demo=python demo_mcp_server.py"')
    sub.add_parser("demo", help="run the scripted demo")
    a = ap.parse_args()
    asyncio.run(chat(a.agent, a.mcp) if a.cmd == "chat" else demo())


if __name__ == "__main__":
    cli()
