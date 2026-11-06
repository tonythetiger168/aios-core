# Changelog

## 0.2.0 (2026-11-06)

Follows the November 2026 research round-up (MCP 2026-07-28, agent-OS security and scheduling papers).

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
