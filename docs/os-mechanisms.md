# AIOS against classical OS mechanisms

A self-audit, 2026-11-06 (AIOS 0.2.0). Recent research argues that a deployed LLM agent already *is* an
operating system, with ring-0 authority by default: it acts with all of its user's rights, and rules
written into a prompt cannot serve as a reference monitor ("Ring-0 by Default: Agents as Operating
Systems", arXiv 2610.33753, checked 116 textbook mechanisms and found 65 missing in today's coding
agents). This page goes through the same classical subsystems and says, for each, what AIOS
enforces **in code**, what it only partly covers, and what it lacks. The grouping is ours, not the
paper's.

✅ enforced in code · ⚠️ partial · ❌ missing

| Subsystem | OS mechanism | In AIOS | |
|---|---|---|---|
| Processes | process abstraction, own address space | each agent has its own permissions, history, memory and budget; helpers start fresh | ✅ |
| | fork/exec with reduced rights | delegation (`agent__<name>`); `narrow` / `delegate` caps only narrow down the chain | ✅ |
| Scheduling | run queue, worker pool | kernel queue + workers; swarms with backpressure | ✅ |
| | admission control under memory pressure | Docker/microVM sandboxes start only while their limits fit in free memory (`aios_admit`) | ✅ |
| | priorities, preemption | none: model slots are first come, first served | ❌ |
| Memory (context) | bounded working set | history limit, compaction into a summary, old images thinned | ✅ |
| | paging in on demand | `search_memory`; `recall_on` attaches matching notes after chosen tool calls | ⚠️ (keyword match, no learned prefetch) |
| | integrity of what is paged in | provenance on every note (`user`/`model`/`tainted`); unverified notes taint the run | ✅ |
| Protection | reference monitor outside the untrusted code | permissions, scopes, argument rules and approvals are checked by the kernel, never by the model | ✅ |
| | least privilege by default | tools invisible unless granted; MCP tools need approval or a scope | ✅ |
| | protection of the user's other files | workspace-only file tools; `run_python` sandboxed | ✅ |
| | | MCP servers run with your rights unless `--confine-mcp` / `--offline-mcp` / `--mcp-egress` | ⚠️ |
| Information flow | taint tracking | a run that read untrusted content loses every auto-approval | ✅ |
| | per-value labels | argument provenance: a risky call whose arguments repeat untrusted text needs a person | ⚠️ (verbatim copies only; reworded values are not caught) |
| | declassification | a person, at the approval prompt | ✅ |
| Identity | users and authentication | A2A bearer tokens, console bound to localhost, Telegram allow-list | ⚠️ |
| | multi-user accounts, RBAC | single-user | ❌ (planned for Team/Cloud) |
| IPC | messages between processes | delegation, swarms, A2A (1.0 and 0.3), MCP client and server (2026-07-28 and older), webhooks | ✅ |
| Devices / drivers | drivers isolated from the kernel | MCP servers as stdio children, optionally confined, offline or egress-limited | ✅ |
| Network | firewall, egress filtering | `fetch_url` blocks private addresses and redirects; `--mcp-egress` allow-list proxy with audit | ✅ |
| Isolation | sandboxes | process (rlimits, Landlock, seccomp, netns), macOS sandbox-exec, Windows AppContainer, Docker, Firecracker microVM | ✅ |
| Resources | quotas and accounting | per-run and daily token/cost budgets, sliding-window rate limits, call caps, rlimits | ✅ |
| | loop / runaway detection | loop guard (`--max-repeats`) | ✅ |
| Audit | tamper-evident logging | hash-chained audit, external anchors, forwarding sinks, watchdog alerts | ✅ |
| | export for outside review | `aios export` (OpenAI-format chat JSON, from history or the audit log) | ✅ |
| Services | init, supervision, health | daemon, service install, heartbeat, `aios doctor` / `status` | ✅ |
| Software installation | signed packages, pinned versions | skills pinned by digest, scanned, OpenSSH publisher signatures, a static registry (optionally signed) | ✅ |
| Recovery | transactions, undo | checkpoints of every `write_file`, `aios undo` | ⚠️ (MCP side effects cannot be undone) |
| Time | timers, cron | schedules, file-change and webhook triggers | ✅ |

## What the gaps mean

- **No priorities or preemption.** A long helper cannot be paused for an urgent request; model calls
  wait in arrival order. Acceptable for one person's machine; it matters for a shared server.
- **Provenance, not meaning.** The paper "When the Agent Becomes the Kernel" (arXiv 2609.23700)
  shows why checks on *where data came from* can be deterministic while checks on *what text means*
  always leave a residue. AIOS follows that split: taint and argument provenance are deterministic,
  and everything that would need judging meaning (is this request what the user wanted?) goes to a
  person. A model rewording injected text defeats the verbatim check, which is why tainted runs
  still need a person for every risky call by default.
- **MCP servers are trusted code unless confined.** The MCP security best practices for 2026-07-28
  only *recommend* sandboxing spawned servers; AIOS offers it per server, but does not force it.
- **Single user.** Identity is a token per caller, not accounts with roles.

## Sources

- Ring-0 by Default: Agents as Operating Systems (AaOS), arXiv 2610.33753, 2026-10-24.
- When the Agent Becomes the Kernel: A Systematization of Security on the Path to AI-Native Operating Systems, arXiv 2609.23700, 2026-09-20.
- Rethinking Access Control for AI Agents: Recent Advances and Open Challenges, arXiv 2611.06764, 2026-11-04.
- TenonAgent, arXiv 2611.00051; ToolSched, arXiv 2611.05269; MemSidecar, arXiv 2611.00779.
- Model Context Protocol, specification 2026-07-28 and its security best practices.
