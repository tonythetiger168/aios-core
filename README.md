# aios-core

> Secure-by-default, local-first runtime for AI agents: scheduler, permissions, persistent memory, sandboxed tools and MCP. Python.

**繁體中文：[README.zh-TW.md](README.zh-TW.md)**

**Status: early alpha.** This is a working prototype, not a hardened product. Read [Security status](#security-status) before giving it access to anything you care about.

## Why

Agents need an operating-system-like layer: who may call which tool, what is remembered, what runs in isolation, what gets logged. Big vendors are building this into their own OS or enterprise platform. The popular open-source personal agents proved the demand but shipped with weak security (see [Landscape](#landscape)). `aios-core` aims at the gap: a **local-first, cross-platform, model-agnostic runtime for individuals and small teams, secure by default**.

"Model-agnostic" is the goal; today only Claude is wired in (see roadmap P0).

## What works today

- **Kernel**: async task queue with a worker pool; each agent is a "process" with its own permissions, history and memory.
- **Tool registry** ("syscalls") with JSON-schema tools; an agent only sees tools it is permitted to use (exact name or `prefix*` wildcard).
- **Persistent memory** (SQLite): conversation history survives restarts; long-term facts via `remember` / `recall` / `list_facts` and are injected into the system prompt.
- **Workspace file tools** (`read_file`, `write_file`, `list_files`), path-escape protected, UTF-8.
- **Sandboxed code execution** (`run_python`): separate process, CPU/memory/file-size limits, no network (Linux with unprivileged user namespaces). Disabled by default on Windows.
- **MCP client**: attach stdio MCP servers; their tools are registered as `<server>__<tool>` and are *not* granted to agents unless you allow them.
- **CLI**: interactive chat with `/facts`, `/reset`, `/quit`.

## Quick start

Requires Python 3.10+ and an [Anthropic API key](https://platform.claude.com/settings/keys).

```bash
pip install anthropic            # optional: pip install mcp
export ANTHROPIC_API_KEY="sk-ant-..."      # PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."

python aios.py chat                         # interactive agent named "main"
python aios.py chat --agent work            # separate history + memory per name
python aios.py chat --mcp "demo=python demo_mcp_server.py"   # attach an MCP server
python aios.py demo                         # scripted demo
```

State is stored in `./aios.db` and `./workspace/` (both git-ignored).

## Architecture

```
 you ──> CLI ──> Kernel ──┬─> Claude API (tool-use loop, per-agent permissions)
                          ├─> ToolRegistry ──> built-in tools / MCP servers
                          ├─> Store (SQLite): history, long-term facts
                          └─> Sandbox: subprocess + rlimits + no network
```

## Built-in tools

| Tool | What it does |
|---|---|
| `now` | Current local date and time |
| `read_file` / `write_file` / `list_files` | Workspace-only file access |
| `remember` / `recall` / `list_facts` | Per-agent long-term memory |
| `run_python` | Sandboxed Python (Linux; opt-in on Windows) |

## Security status

Honest list of what this does **not** do yet:

- **No defence against prompt injection** from untrusted content (web pages, files, MCP results). Do not combine untrusted content with high-risk tools.
- **No human approval step** and **no audit log** yet.
- **The sandbox is not a full security boundary.** It limits CPU, memory and network on Linux, but code can still read files its user can read, and process count is unlimited. For untrusted code use WSL2/Docker/a microVM.
- **Windows**: `run_python` is disabled (timeout-only isolation is not safe). Set `AIOS_ALLOW_WEAK_SANDBOX=1` to enable it at your own risk.
- Treat MCP servers as untrusted code and untrusted data sources; grant them per agent explicitly.
- No cost budget beyond an 8-step limit per run.

## Roadmap

Months are relative estimates (month 1 = project start) and will move.

| Phase | When | Goal | Exit criterion |
|---|---|---|---|
| **P0 Foundations** | month 1 | Automated tests · audit log · human approval for risky tools · cost/step budgets · model-provider abstraction (incl. local models) | Every action is traceable; budget overruns stop the run |
| **P1 Secure core** | months 1–2 | Capability-based permissions · untrusted-content tagging · skill signing & scanning · memory v2 (full-text + vector, temporal facts) · run as an MCP server · prompt-injection test suite | Passes our own injection test suite |
| **P2 Expansion** | months 3–4 | Multi-agent with A2A · schedules/triggers · local web console · pluggable sandbox backends (WSL2 / Docker / microVM) | Multi-agent in one command; real isolation on Windows |
| **P3 Ecosystem** | months 6–9 | Signed skill registry · optional hosted version | External contributors ship skills |
| **P4 OS layer** | month 12+ | WSL image / Linux distro · system service · mobile companion | Decide based on earlier results |

Explicit non-goal for now: writing an actual OS kernel.

Detailed tracking (gap analysis, per-item status, Gantt chart): [`docs/landscape-and-roadmap.xlsx`](docs/landscape-and-roadmap.xlsx).

## Product line (planned)

| Product | What | Phase | Model |
|---|---|---|---|
| **AIOS Core** | This repo: scheduler, permissions, memory, MCP | P0–P1 | Open source |
| **AIOS Shell** | CLI (exists) + local web console with approvals and audit view | P2 | Open source |
| **AIOS Guard** | Policy engine, skill scanner, prompt-injection test suite | P1 | TBD (likely the paid differentiator) |
| **AIOS Memory** | Standalone memory service with an MCP interface | P1 | TBD |
| **AIOS Sandbox** | Multi-backend isolated execution | P2 | TBD |
| **AIOS Hub** | Signed skills / agents registry | P3 | TBD |
| **AIOS Team / Cloud** | Hosted, RBAC, audit export | P3 | Paid |

Business-model rows are hypotheses, not commitments.

## Landscape

A snapshot as of October 2026. Much of it is vendor-published or press coverage; Apple items are reports, not announcements.

- **Security is the open wound.** Gartner analysts called OpenClaw's risk "unacceptable" ([Security Boulevard](https://securityboulevard.com/2026/03/latest-openclaw-flaw-can-let-malicious-websites-hijack-local-ai-agents/)); researchers report that poisoning any one of an agent's capability, identity or knowledge raises attack success from 24.6% to 64–74% ([ICML 2026](https://icml.cc/virtual/2026/69297)).
- **OS vendors are building it in**: Windows has an on-device MCP registry, agent workspace and agent identities ([TechSpective](https://techspective.net/2025/11/18/windows-becoming-operating-system-for-ai-agents/)); Android has AppFunctions and Gemini Intelligence ([MacRumors](https://macrumors.com/2026/05/12/google-previews-android-17/)).
- **Standards settled**: MCP and A2A both sit under the Linux Foundation's Agentic AI Foundation ([source](https://algeriatech.news/?p=35142)).
- Full list with sources: [`docs/landscape-and-roadmap.xlsx`](docs/landscape-and-roadmap.xlsx).

## Contributing

Issues and PRs welcome, especially: tests, sandbox backends, prompt-injection test cases, and provider adapters.

## License

MIT, see [LICENSE](LICENSE).
