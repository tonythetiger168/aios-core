"""Export what agents did as OpenAI-format chat JSON, for scanners and review tools outside AIOS.

    python aios.py export --agent main --home ~/aios-data > main.json      # saved chat history
    python aios.py export --agent job-digest --from-audit -n 5 --home ...  # rebuilt from the audit log

Many agent-security tools (Cisco's Project Quarry scanner, LLM-judge evaluators, red-team graders)
read a conversation as a list of chat-completions messages: system/user/assistant with tool_calls,
and tool results. This module writes that shape from AIOS's own records, offline, so such a tool
can look at a run without being wired into AIOS.

Two sources:
- the saved history of a chat agent (the `messages` table, the newest HISTORY_LIMIT messages): the
  real conversation, untrusted tool output still wrapped in its <untrusted> tags;
- the audit log (`--from-audit`), which also covers runs that keep no history (jobs, helpers, A2A,
  swarm workers): each run becomes the task, its tool calls with their (clipped) results, and the
  run summary. Refused calls stay in, with the refusal as their result and `"status"` beside it.

Images are replaced by a short note, since scanners read text and the base64 would be huge.
"""
import json

import aios


def _no_media(msgs: list[dict]) -> list[dict]:
    for m in msgs:
        if isinstance(m.get("content"), list):
            m["content"] = [p if p.get("type") != "image_url" else {"type": "text", "text": "[image omitted]"}
                            for p in m["content"]]
    return msgs


def from_history(store, agent: str) -> list[dict]:
    """The agent's saved conversation, without the system prompt (it is not stored)."""
    msgs = aios.OpenAICompatProvider.to_wire_messages("", store.load_messages(agent))[1:]
    return _no_media(msgs)


def from_audit(store, agent: str, runs: int = 10) -> list[dict]:
    """The agent's newest `runs` runs, rebuilt from its audit entries (oldest first)."""
    rows = store.db.execute("SELECT id, ts, tool, args, result, status FROM audit WHERE agent=? ORDER BY id",
                            (agent,)).fetchall()
    out_runs, calls = [], []
    for rid, ts, tool, args, result, status in rows:
        try:
            parsed = json.loads(args) if args else {}
        except ValueError:
            parsed = {"raw": args}
        if tool == aios.RUN_ENTRY:
            run = [{"role": "user", "content": parsed.get("task", ""), "ts": ts}]
            for i, (cid, cts, ctool, cargs, cres, cst) in enumerate(calls):
                call_id = f"audit-{cid}"
                run.append({"role": "assistant", "content": None, "ts": cts, "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": ctool, "arguments": json.dumps(cargs, ensure_ascii=False)}}]})
                run.append({"role": "tool", "tool_call_id": call_id, "content": cres or "", "status": cst})
            run.append({"role": "assistant", "content": f"(run ended: {result})", "status": status, "ts": ts})
            out_runs.append(run)
            calls = []
        elif not tool.startswith("("):  # tool calls; skip (compact), (provenance), (recall), ...
            calls.append((rid, ts, tool, parsed, result, status))
    return [m for run in out_runs[-runs:] for m in run] if runs else []


def run(home: str, agent: str, audit: bool, runs: int, out_path: str | None) -> int:
    from pathlib import Path
    db = Path(home).resolve() / "aios.db"
    if not db.exists():
        print(f"no data: {db} does not exist")
        return 1
    store = aios.Store(str(db))
    try:
        msgs = from_audit(store, agent, runs) if audit else from_history(store, agent)
    finally:
        store.db.close()
    text = json.dumps(msgs, ensure_ascii=False, indent=1)
    if out_path:
        Path(out_path).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {len(msgs)} messages to {out_path}")
    else:
        print(text)
    return 0 if msgs else 1
