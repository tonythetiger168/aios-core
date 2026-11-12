# Changelog

## Unreleased

- **Pinned local model weights** (`--model-digest`, `$AIOS_MODEL_DIGEST`): with the openai provider on
  Ollama, the model's digest must match the pin before the first call and every 10 minutes after,
  or the run stops without calling it (a re-pulled or replaced model can carry a backdoor that
  ordinary evals never trigger). `aios doctor` lists local Ollama models with their digests.
- **Hardening from the Oct-Nov 2026 agent-security incidents**:
  - Audit log: values with the shape of a known credential (GitHub, OpenAI, Anthropic, AWS, Slack,
    Google, GitLab, Hugging Face tokens, PEM private keys) are masked as `[redacted]` in arguments and
    tool results alike, before the hash is computed.
  - Self-improvement: lessons that loosen oversight (monitors, alerts, watchdog, supervisor, hooks,
    `--no-verify`, `--force`, skipping tests or checks) are flagged and wait for a person.
  - Skills: `aios skills scan` reports `loosens-oversight` (medium) for such directives.
  - Scopes: a path with a trailing dot or space in any part, or a `:` (an NTFS data stream), matches
    no `paths` / `auto_paths` rule, because Windows would open a different name than the rule saw;
    on Windows the rules ignore case like NTFS does.
- **Audit log**: argument values whose keys look like secrets (`password`, `token`, `api_key`, ...) are stored as `[redacted]` before the hash is computed. The tool still receives the real arguments. Tool results are stored as returned.
- **3D office** (`aios_office`, console → Office): agents walk between an open office, library, server
  room, talk lounge (two agents talking, with speech bubbles), meeting room (discussions of three or
  more), lobby (reception, approval counter, waiting area), security office and break room according
  to their live state. Dependency-free canvas renderer; the page's own CSP allows only its fixed script.
- **Agents as chat bots** (`aios_bot`, `aios bot`): a policy agent answers in Telegram chats when it is
  @mentioned, asked with `/ask` or replied to, and replies in the thread. It sees the message it was
  tagged under, the earlier exchange of a reply thread and, with `--context N`, recent chat messages.
  Chat members are strangers: each question is a fresh tainted run (read-only without a policy, nobody
  can approve), only `--allow-chat` chats (or `--public`), `--per-user` answers an hour, a 20k-token
  cap per answer and a 200k-token daily budget by default.
- **Self-improvement, live** (`aios_improve`, `aios improve`): each policy agent has a versioned playbook
  of lessons learned from its own audit log (refused, failing and repeated calls, runs that ran out of
  steps). A candidate is run on your eval tasks next to the current version, on a throwaway copy of
  aios.db and the workspace, and goes live from the next run only if it does at least as well;
  `rollback` goes back, and a version whose runs fail more is rolled back automatically. `improve loop`
  repeats this with no round limit; quiet rounds make no model call, evals use only a free local model
  unless a USD cap is set, and `--daily-tokens` pauses it with an alert. Tainted or guardrail-related
  lessons wait for a person. Agents can file code or policy patches (`propose_patch`) that only a
  person applies (`approve-patch`), with a health check that reverses a failing patch.

- **Self-supervision** (`aios_supervisor`, `aios supervisor`): the daemon answers its own watchdog alerts.
  Failing, budget-burning or injected-looking jobs are paused, other agents are held (every risky call
  needs a person), and a broken audit chain puts everything in safe mode. Rule-based, no model calls; it
  only ever tightens, and only `aios supervisor --release` (a person) undoes an action.
- **Site**: the homepage leads with self-improvement and has sections for self-supervision, the chat
  bot and the 3D office (with a screenshot).

## 0.2.0 (2026-11-06)

Follows the November 2026 research round-up (MCP 2026-07-28, agent-OS security and scheduling papers).

- **Swarms over many machines** (`aios_node`): `aios swarm --nodes [HOST:]PORT` runs the swarm as the
  head of a cluster and `aios node --head URL` joins a machine as a worker. Workers pull tasks by their
  free slots, send heartbeats, and lose their tasks to other nodes after 30 s of silence (at most 3
  tries, first answer wins). Every call needs `$AIOS_NODE_TOKEN`; a head on a non-local address needs
  TLS (`--node-cert`/`--node-key`, `--head-ca` on nodes). Each node runs tasks as its own policy agent
  and audits them; the head's swarm budget covers the whole cluster.
- **Moving a run between nodes**: an agent with `transfer = true` can hand a run in progress to
  another node of the same swarm instead of starting over.
- **Swarm memory guard**: `--parallel` is lowered to what free memory holds, new helpers wait while
  free memory is under `AIOS_SWARM_MIN_FREE_MB` (256), and answers kept for `--reduce` spill to disk,
  so memory stays flat for any task count.
- **MCP 2026-07-28**: aios connects to MCP servers with the stateless `server/discover` when the SDK
  supports it (mcp 2.3+) and falls back to the `initialize` handshake for older servers; the revision
  each server got is audited; `aios serve` works with 2026-07-28 clients and reports its version.
- **Argument provenance**: in a tainted run, a risky call whose arguments repeat untrusted text (or a
  URL host from it) loses every auto rule and needs a person; audited as `(provenance)`, alert
  `untrusted-args`.
- **Sandbox admission control**: Docker and microVM sandboxes start only while their memory limits fit
  in the host's free memory (`AIOS_SANDBOX_RESERVE_MB`, `AIOS_SANDBOX_MAX`, `AIOS_SANDBOX_WAIT`).
- **Delegation caps**: `narrow = true` and `[agents.X.delegate]` let a caller cut down what its helpers
  may use, down the whole chain. Agent sections now reject unknown keys.
- **`recall_on`**: attach matching memory notes after chosen tool calls, once per run, audited as
  `(recall)`.
- **`aios export`**: an agent's conversation, or runs rebuilt from the audit log, as OpenAI-format chat
  JSON for outside scanners.
- **Docs**: `docs/os-mechanisms.md` (and zh-TW), a self-audit against classical OS mechanisms.

## 0.1.0

First packaged release: kernel, policy and scopes, taint, sandboxes, MCP, A2A, swarms, schedules,
console, skills registry, editions.
