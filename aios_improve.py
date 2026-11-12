"""Self-improvement: agents learn from their own runs, and what they learn takes effect live.

Each policy agent has a *playbook*: a short, versioned list of lessons that goes into its prompt.
A new version is proposed from the agent's own audit log (refused, failed and looping tool calls,
runs that ran out of steps or budget), or suggested by the agent itself with the `suggest_lesson`
tool. A proposal is only a candidate. It replaces the active version only after it has been run
against the person's eval tasks next to the current version and done at least as well; then the
very next run of that agent uses it (the prompt is rebuilt for every run, so nothing restarts).
`aios improve rollback` goes back one version, and a version whose runs get worse after it went
live is rolled back automatically.

What this can never do by itself: a playbook is prompt text. It grants no tool, path or argument;
policy, permissions, scopes, approvals, audit and the AIOS code are untouched by it. Lessons that
talk about those, and lessons suggested while a run had read untrusted content, are never adopted
without a person (`aios improve adopt`). An agent that wants a change to code or policy files files
a patch with `propose_patch`; only a person applies it (`aios improve approve-patch`), after which
a health check runs and a failing patch is reversed on the spot.

`aios improve loop` repeats propose -> evaluate -> adopt with no limit on rounds. A round with no
new failures since the last one costs nothing (no model call), and eval spending has its own
24-hour token / cost cap: when it is reached the loop pauses and raises an alert. Evals only run on
a free model by default (a model server on this machine, such as Ollama): with a paid one a round
skips its evals unless a spending cap in USD (--daily-cost) was set on purpose.
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

MAX_LESSONS = 20  # lessons kept in one playbook (the newest win)
LESSON_MAX = 300  # characters of one lesson
PATCH_MAX = 100_000  # bytes of one proposed patch
WORKSPACE_COPY_MAX = 100 * 1024 * 1024  # evals run on a copy of the workspace up to this size
BAD = ("denied", "error", "loop")  # tool-call statuses that count as mistakes
HEALTH_MIN_RUNS = 5  # runs after adoption before the automatic health check judges a version
HEALTH_MARGIN = 0.2  # failure rate may rise this much before a version is rolled back
DAILY_TOKENS = 50_000  # default eval spending cap of `aios improve loop` per 24 hours
AUDIT_AGENT = "improve"

# Words that mean a lesson is about the guardrails rather than the task. Such lessons are flagged
# and need a person, however well they score.
GUARD = re.compile(r"permission|polic|approv|audit|sandbox|scope|taint|untrusted|bypass|disabl|"
                   r"ignore (all|any|previous|earlier|prior)|override|jailbreak|system prompt|"
                   r"auto_paths|auto_args|auto_when|grant|credential|secret|token budget|"
                   # oversight: agents have been seen silencing alerts and removing their own monitor
                   r"monitor|alert|watchdog|supervis|\bhooks?\b|no-verify|--force|"
                   r"skip\w*\W+(the\s+)?(tests?|checks?|verif\w*|review\w*)|turn(ing|s)?\s+off|silenc|suppress", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS playbooks(
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, version INTEGER, body TEXT, status TEXT,
    source TEXT, tainted INTEGER DEFAULT 0, flagged TEXT DEFAULT '', ts TEXT, decided_ts TEXT,
    note TEXT DEFAULT '', UNIQUE(key, version));
CREATE TABLE IF NOT EXISTS improve_evals(
    key TEXT, version INTEGER, cases_sha TEXT, passed INTEGER, total INTEGER, bad_calls INTEGER,
    tokens INTEGER, ts TEXT, PRIMARY KEY(key, version, cases_sha));
CREATE TABLE IF NOT EXISTS improve_spend(ts TEXT, key TEXT, tokens INTEGER, cost REAL);
CREATE TABLE IF NOT EXISTS improve_state(key TEXT PRIMARY KEY, last_round TEXT);
CREATE TABLE IF NOT EXISTS improve_patches(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, agent TEXT, path TEXT, diff TEXT, reason TEXT,
    tainted INTEGER, status TEXT DEFAULT 'pending', decided_ts TEXT, detail TEXT DEFAULT '');
"""

now_iso = lambda: datetime.now().isoformat(timespec="seconds")


def ensure(db: sqlite3.Connection):
    db.executescript(SCHEMA)


def _audit(store, tool: str, args: dict, result: str, status: str = "ok"):
    store.log_audit(AUDIT_AGENT, tool, args, result, status)


# ---- lessons and playbooks ----

def clean_lesson(text: str) -> str:
    """One line, no list markers, at most LESSON_MAX characters (newlines cannot forge structure)."""
    return " ".join(str(text).split()).lstrip("-*• ").strip()[:LESSON_MAX]


def lessons(body: str) -> list[str]:
    return [ln[2:] for ln in (body or "").splitlines() if ln.startswith("- ")]


def make_body(items) -> str:
    seen, out = set(), []
    for it in items:
        it = clean_lesson(it)
        if it and it.lower() not in seen:
            seen.add(it.lower())
            out.append(it)
    return "\n".join(f"- {x}" for x in out[-MAX_LESSONS:])


def flags(body: str) -> str:
    """The lessons that touch guardrails, joined; '' when none does."""
    return " | ".join(x for x in lessons(body) if GUARD.search(x))


def _row(r) -> dict | None:
    if not r:
        return None
    keys = ("id", "key", "version", "body", "status", "source", "tainted", "flagged", "ts", "decided_ts", "note")
    d = dict(zip(keys, r))
    d["tainted"] = bool(d["tainted"])
    return d


_COLS = "id, key, version, body, status, source, tainted, flagged, ts, decided_ts, note"


def active(db, key: str) -> dict | None:
    ensure(db)
    return _row(db.execute(f"SELECT {_COLS} FROM playbooks WHERE key=? AND status='active'", (key,)).fetchone())


def get(db, key: str, version: int) -> dict | None:
    ensure(db)
    return _row(db.execute(f"SELECT {_COLS} FROM playbooks WHERE key=? AND version=?", (key, version)).fetchone())


def versions(db, key: str | None = None) -> list[dict]:
    ensure(db)
    q, p = f"SELECT {_COLS} FROM playbooks", ()
    if key:
        q, p = q + " WHERE key=?", (key,)
    return [_row(r) for r in db.execute(q + " ORDER BY key, version", p)]


def open_candidate(db, key: str) -> dict | None:
    ensure(db)
    return _row(db.execute(f"SELECT {_COLS} FROM playbooks WHERE key=? AND status='candidate' "
                           "ORDER BY version DESC LIMIT 1", (key,)).fetchone())


def add_candidate(db, key: str, new_lessons, source: str, tainted: bool = False, note: str = "") -> dict | None:
    """A candidate = the active lessons + new ones. Merges into an open candidate of the same key
    (a candidate is tainted if anything in it is). None when nothing new would be added."""
    ensure(db)
    base = open_candidate(db, key) or active(db, key)
    old = lessons(base["body"]) if base else []
    body = make_body(old + list(new_lessons))
    if body == make_body(old):
        return None
    cand = open_candidate(db, key)
    if cand:
        tainted = tainted or cand["tainted"]
        source = cand["source"] if source in cand["source"].split("+") else cand["source"] + "+" + source
        db.execute("UPDATE playbooks SET body=?, source=?, tainted=?, flagged=?, note=? WHERE id=?",
                   (body, source, int(tainted), flags(body), (cand["note"] + " " + note).strip(), cand["id"]))
        db.execute("DELETE FROM improve_evals WHERE key=? AND version=?", (key, cand["version"]))  # stale
    else:
        v = (db.execute("SELECT MAX(version) FROM playbooks WHERE key=?", (key,)).fetchone()[0] or 0) + 1
        db.execute("INSERT INTO playbooks(key, version, body, status, source, tainted, flagged, ts, note) "
                   "VALUES(?,?,?,?,?,?,?,?,?)", (key, v, body, "candidate", source, int(tainted), flags(body),
                                                 now_iso(), note))
    db.commit()
    return open_candidate(db, key)


def prompt_block(store, key: str, override: dict | None = None) -> str | None:
    """The playbook text for an agent's system prompt; read on every run, so an adopted version is
    used from the next run on. override: {key: body or None} (evals)."""
    if override is not None and key in override:
        body = override[key]
    else:
        pb = active(store.db, key)
        body = pb["body"] if pb else None
        if pb:
            body = f"(playbook v{pb['version']})\n" + body
    if not body:
        return None
    return ("Lessons from your earlier runs (checked before use; they help with how you work and "
            "change nothing about what you may do):\n" + body)


# ---- signals: what went wrong lately ----

def agent_names(db, key: str) -> list[str]:
    """Audit names of a policy agent: itself and its scheduled jobs (job-<name>)."""
    try:
        jobs = [r[0] for r in db.execute("SELECT name FROM jobs WHERE agent=?", (key,))]
    except sqlite3.OperationalError:
        jobs = []
    return [key] + [f"job-{j}" for j in jobs]


def signals(db, key: str, since: str | None) -> dict:
    """Counts only (tool names, statuses, stop reasons): no tool output, so nothing an attacker
    wrote can travel from here into a lesson."""
    names = agent_names(db, key)
    marks = ",".join("?" * len(names))
    rows = db.execute(f"SELECT tool, status FROM audit WHERE agent IN ({marks}) AND ts>?",
                      (*names, since or "")).fetchall()
    calls, stops, runs = Counter(), Counter(), 0
    for tool, status in rows:
        if tool == "(run)":
            runs += 1
            if status != "ok":
                stops[status] += 1
        elif status in BAD:
            calls[(tool, status)] += 1
    return {"runs": runs, "calls": calls, "stops": stops}


def heuristic_lessons(sig: dict) -> list[str]:
    out = []
    for (tool, status), n in sorted(sig["calls"].items(), key=lambda kv: (-kv[1], kv[0])):
        if status == "denied":
            out.append(f"{tool} was refused {n} time(s): do not try a refused call again; say what you "
                       "need and why instead.")
        elif status == "error":
            out.append(f"{tool} failed {n} time(s): check its arguments against its description before "
                       "calling it, and read the error before trying again.")
        elif status == "loop":
            out.append(f"{tool} was called again with the same arguments {n} time(s): reuse the earlier "
                       "result instead of repeating the call.")
    for stop, n in sorted(sig["stops"].items()):
        if stop in ("max_steps", "budget"):
            out.append(f"{n} run(s) ran out of {'steps' if stop == 'max_steps' else 'budget'}: plan first, "
                       "use fewer tool calls, and answer as soon as you have enough.")
        elif stop == "loop":
            out.append(f"{n} run(s) were stopped for repeating themselves: when a call does not help, "
                       "change approach or answer with what you have.")
        elif stop == "rate":
            out.append(f"{n} run(s) hit the rate limit: batch the work into fewer model calls.")
        elif stop == "error":
            out.append(f"{n} run(s) ended in an error: keep steps small and check results as you go.")
    return out


async def model_lessons(provider, sig: dict, current: list[str], max_tokens: int = 600) -> tuple[list[str], int]:
    """Lessons written by the model from the counts (no free text from runs goes in)."""
    facts = {"runs": sig["runs"], "stops": dict(sig["stops"]),
             "tool_mistakes": [{"tool": t, "status": s, "count": n} for (t, s), n in sig["calls"].items()]}
    system = ("You improve an AI agent's working habits. From the statistics of its recent runs, write at most "
              "5 short, concrete lessons, one per line, each starting with '- '. Lessons are about how to do "
              "tasks well. Never write about permissions, policies, approvals, sandboxes or audit.")
    msg = (f"Current lessons:\n{make_body(current) or '(none)'}\n\nRecent statistics:\n"
           f"{json.dumps(facts, ensure_ascii=False)}\n\nNew lessons:")
    comp = await provider.complete(system, [{"role": "user", "content": msg}], [], max_tokens=max_tokens)
    text = "".join(b.get("text", "") for b in comp.content if b.get("type") == "text")
    return [ln[2:] for ln in text.splitlines() if ln.startswith("- ")][:5], comp.input_tokens + comp.output_tokens


def last_round(db, key: str) -> str | None:
    ensure(db)
    r = db.execute("SELECT last_round FROM improve_state WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def mark_round(db, key: str, ts: str):
    db.execute("INSERT OR REPLACE INTO improve_state(key, last_round) VALUES(?,?)", (key, ts))
    db.commit()


async def propose(store, key: str, since: str | None = None, provider=None) -> tuple[dict | None, dict, int]:
    """(candidate or None, signals, tokens spent). since defaults to the last round."""
    ensure(store.db)
    sig = signals(store.db, key, since if since is not None else last_round(store.db, key))
    new, spent = heuristic_lessons(sig), 0
    if provider is not None and (sig["calls"] or sig["stops"]):
        base = active(store.db, key)
        more, spent = await model_lessons(provider, sig, lessons(base["body"]) if base else [])
        new += more
    cand = add_candidate(store.db, key, new, "model" if provider else "audit") if new else None
    if cand:
        _audit(store, "propose", {"agent": key, "version": cand["version"]}, f"{len(lessons(cand['body']))} lessons")
    return cand, sig, spent


# ---- evaluation ----

def load_cases(path: str) -> tuple[list[dict], str]:
    raw = Path(path).read_bytes()
    cases = []
    for i, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        c = json.loads(line) if line.startswith("{") else {"task": line}
        if not isinstance(c.get("task"), str) or not c["task"].strip():
            raise ValueError(f"{path}:{i}: each case needs a task")
        for opt in ("expect", "forbid"):
            if opt in c:
                re.compile(c[opt])
        cases.append(c)
    if not cases:
        raise ValueError(f"{path}: no eval cases")
    return cases, hashlib.sha256(raw).hexdigest()[:16]


def passed(case: dict, answer: str) -> bool:
    if answer.startswith("(stopped") or answer == "(max steps reached)":
        return False
    if "expect" in case and not re.search(case["expect"], answer, re.I | re.S):
        return False
    return not ("forbid" in case and re.search(case["forbid"], answer, re.I | re.S))


def _copy_workspace(src: Path, dst: Path):
    if not src.exists():
        dst.mkdir(parents=True)
        return
    size = sum(p.stat().st_size for p in src.rglob("*") if p.is_file())
    if size > WORKSPACE_COPY_MAX:
        raise ValueError(f"workspace is {size // 2**20} MB; evals copy at most {WORKSPACE_COPY_MAX // 2**20} MB "
                         "(point --home at a smaller test folder)")
    shutil.copytree(src, dst)


async def evaluate(store, key: str, body: str | None, cases: list[dict], provider, budget,
                   policy: dict | None = None, workspace: Path | None = None) -> dict:
    """Run every case once with `body` as the playbook, on a throwaway copy of aios.db and of the
    workspace: memory, audit, rate counters and files of the real setup are left as they were.
    Nobody answers prompts, so risky calls are refused unless the policy pre-allows them."""
    import aios
    mem = aios.Store(":memory:")
    store.db.commit()
    store.db.backup(mem.db)
    old_ws = aios.WORKSPACE
    with tempfile.TemporaryDirectory(prefix="aios-eval-") as tmp:
        ws = Path(tmp) / "workspace"
        _copy_workspace(workspace or old_ws, ws)
        aios.WORKSPACE = ws
        k = aios.Kernel(store=mem, provider=provider, budget=budget)
        k.playbook_override = {key: body}
        try:
            aios.install_builtin_tools(k, "process" if os.name != "nt" else None)
            aios.install_agent_tools(k, policy)
            pol = (policy or {}).get(key) or {"permissions": None, "scopes": {}}
            perms = set(pol["permissions"]) if pol.get("permissions") is not None else set(aios.DEFAULT_TOOLS)
            start = now_iso()
            ok = 0
            for c in cases:
                agent = aios.Agent(key, perms, scopes=pol.get("scopes") or {}, rate=pol.get("rate") or {},
                                   loaded=True, ephemeral=True)
                try:
                    answer = await k._run(agent, c["task"])
                except Exception as e:  # a crashing case is a failed case
                    answer = f"(stopped: {type(e).__name__}: {e})"
                ok += passed(c, answer)
            bad = mem.db.execute("SELECT COUNT(*) FROM audit WHERE agent=? AND ts>=? AND status IN (?,?,?) "
                                 "AND tool!='(run)'", (key, start, *BAD)).fetchone()[0]
            tokens, cost = mem.usage_since(start, aios.like_exact(key))
        finally:
            aios.WORKSPACE = old_ws
            await k.close()
            mem.db.close()
    return {"passed": ok, "total": len(cases), "bad_calls": bad, "tokens": tokens, "cost": cost}


def record_spend(db, key: str, tokens: int, cost: float = 0.0):
    if tokens or cost:
        db.execute("INSERT INTO improve_spend(ts, key, tokens, cost) VALUES(?,?,?,?)", (now_iso(), key, tokens, cost))
        db.commit()


def spent_24h(db) -> tuple[int, float]:
    ensure(db)
    since = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    t, c = db.execute("SELECT COALESCE(SUM(tokens),0), COALESCE(SUM(cost),0) FROM improve_spend WHERE ts>=?",
                      (since,)).fetchone()
    return int(t), float(c)


async def score(store, key: str, version: int, body: str | None, cases, sha: str, provider, budget,
                policy=None, workspace=None) -> dict:
    """Eval result of one version (0 = no playbook), cached per cases file: a baseline that did not
    change is not paid for twice."""
    ensure(store.db)
    r = store.db.execute("SELECT passed, total, bad_calls, tokens FROM improve_evals WHERE key=? AND version=? "
                         "AND cases_sha=?", (key, version, sha)).fetchone()
    if r:
        return {"passed": r[0], "total": r[1], "bad_calls": r[2], "tokens": r[3], "cached": True}
    res = await evaluate(store, key, body, cases, provider, budget, policy, workspace)
    record_spend(store.db, key, res["tokens"], res["cost"])
    store.db.execute("INSERT OR REPLACE INTO improve_evals VALUES(?,?,?,?,?,?,?,?)",
                     (key, version, sha, res["passed"], res["total"], res["bad_calls"], res["tokens"], now_iso()))
    store.db.commit()
    return res


def better(cand: dict, base: dict) -> bool:
    """The gate: no fewer passed cases and no more mistaken tool calls than the current version."""
    return cand["passed"] >= base["passed"] and cand["bad_calls"] <= base["bad_calls"] and cand["total"] > 0


def needs_person(pb: dict) -> str:
    if pb["tainted"]:
        return "it contains a lesson suggested while the run had read untrusted content"
    if pb["flagged"]:
        return "it talks about guardrails: " + pb["flagged"][:200]
    return ""


# ---- adopting and rolling back (live: the next run reads the new version) ----

def adopt(store, key: str, version: int, by: str = "person", note: str = "") -> dict:
    ensure(store.db)
    pb = get(store.db, key, version)
    if not pb:
        raise ValueError(f"{key} has no playbook v{version}")
    if pb["status"] == "active":
        return pb
    with store.db:
        store.db.execute("UPDATE playbooks SET status='retired', decided_ts=? WHERE key=? AND status='active'",
                         (now_iso(), key))
        store.db.execute("UPDATE playbooks SET status='active', decided_ts=?, note=? WHERE id=?",
                         (now_iso(), (pb["note"] + " " + note).strip(), pb["id"]))
    _audit(store, "adopt", {"agent": key, "version": version, "by": by}, note or "adopted")
    return get(store.db, key, version)


def reject(store, key: str, version: int, why: str = "") -> bool:
    ensure(store.db)
    n = store.db.execute("UPDATE playbooks SET status='rejected', decided_ts=?, note=? WHERE key=? AND version=? "
                         "AND status='candidate'", (now_iso(), why, key, version)).rowcount
    store.db.commit()
    if n:
        _audit(store, "reject", {"agent": key, "version": version}, why or "rejected")
    return bool(n)


def rollback(store, key: str, to: int | None = None, why: str = "by a person") -> dict | None:
    """Back to the previous version (or `to`; 0 = no playbook). Returns the now-active version."""
    ensure(store.db)
    cur = active(store.db, key)
    if to is None:
        r = store.db.execute("SELECT version FROM playbooks WHERE key=? AND status='retired' "
                             "ORDER BY decided_ts DESC, version DESC LIMIT 1", (key,)).fetchone()
        to = r[0] if r else 0
    target = get(store.db, key, to) if to else None
    if to and not target:
        raise ValueError(f"{key} has no playbook v{to}")
    with store.db:
        if cur:
            store.db.execute("UPDATE playbooks SET status='rolled_back', decided_ts=?, note=? WHERE id=?",
                             (now_iso(), (cur["note"] + " rolled back: " + why).strip(), cur["id"]))
        if target:
            store.db.execute("UPDATE playbooks SET status='active', decided_ts=? WHERE id=?", (now_iso(), target["id"]))
    _audit(store, "rollback", {"agent": key, "from": cur["version"] if cur else 0, "to": to}, why)
    return active(store.db, key)


def health(store, key: str) -> dict | None:
    """Rolls the active version back when its runs fail clearly more often than the runs before it.
    Returns what happened, or None when there is nothing to judge yet."""
    pb = active(store.db, key)
    if not pb or not pb["decided_ts"]:
        return None
    names = agent_names(store.db, key)
    marks = ",".join("?" * len(names))
    q = f"SELECT status FROM audit WHERE tool='(run)' AND agent IN ({marks}) AND ts{{}}? ORDER BY id {{}} LIMIT ?"
    after = [r[0] for r in store.db.execute(q.format(">=", "ASC"), (*names, pb["decided_ts"], 50))]
    if len(after) < HEALTH_MIN_RUNS:
        return None
    before = [r[0] for r in store.db.execute(q.format("<", "DESC"), (*names, pb["decided_ts"], len(after)))]
    rate = lambda xs: sum(s != "ok" for s in xs) / len(xs) if xs else 0.0
    r_after, r_before = rate(after), rate(before)
    out = {"version": pb["version"], "after": r_after, "before": r_before, "runs": len(after)}
    if r_after > r_before + HEALTH_MARGIN:
        why = f"failure rate {r_after:.0%} over {len(after)} runs, was {r_before:.0%} before v{pb['version']}"
        rollback(store, key, why="health check: " + why)
        import aios_watchdog as wd
        wd.alert(store.db, "improve-rollback", key, why)
        out["rolled_back"] = True
    return out


# ---- one round, and the endless loop ----

def is_free(provider) -> bool:
    """A model server on this machine (Ollama, LM Studio, llama.cpp, a local vLLM) costs no money
    per call; anything else (Anthropic, a remote OpenAI-compatible API) is treated as paid."""
    from urllib.parse import urlparse
    url = getattr(provider, "base_url", None)
    if not url or not hasattr(provider, "to_wire_messages"):
        return bool(getattr(provider, "free", False))  # test doubles declare themselves
    return (urlparse(url).hostname or "") in ("localhost", "127.0.0.1", "::1")


async def round_(store, key: str, cases, sha: str, provider, budget, policy=None, use_model: bool = False,
                 daily_tokens: int | None = DAILY_TOKENS, daily_cost: float | None = None,
                 workspace=None, log=print) -> str:
    """propose -> evaluate -> adopt for one agent. Returns what happened in one word:
    'quiet' (nothing new, free), 'paused' (spending cap), 'skipped' (paid model, no USD cap set),
    'adopted', 'kept', 'held' (needs a person)."""
    ensure(store.db)
    h = health(store, key)
    if h and h.get("rolled_back"):
        log(f"{key}: v{h['version']} rolled back ({h['after']:.0%} failed runs after, {h['before']:.0%} before)")
    started = now_iso()
    cand = open_candidate(store.db, key)
    sig = signals(store.db, key, last_round(store.db, key))
    if not cand and not (sig["calls"] or sig["stops"]):
        mark_round(store.db, key, started)
        return "quiet"
    paid = not is_free(provider)
    if paid and daily_cost is None:
        why = ("evals skipped: the model is paid and no USD cap (--daily-cost) was set; use a local model "
               "(--provider openai --model gemma4:e4b) to improve for free")
        if sig["calls"] or sig["stops"]:
            await propose(store, key)  # counting the audit log is free; the candidate waits
        mark_round(store.db, key, started)
        log(f"{key}: {why}")
        return "skipped"
    tokens, cost = spent_24h(store.db)
    if (daily_tokens is not None and tokens >= daily_tokens) or (daily_cost is not None and cost >= daily_cost):
        why = (f"self-improvement paused: eval spending in the last 24h is {tokens} tokens / ${cost:.4f} "
               f"(cap {daily_tokens} tokens" + (f", ${daily_cost:.4f}" if daily_cost is not None else "") + ")")
        import aios_watchdog as wd
        wd.alert(store.db, "improve-paused", key, why)
        log(f"{key}: {why}")
        return "paused"
    if sig["calls"] or sig["stops"]:
        new, _, spent = await propose(store, key, provider=provider if use_model else None)
        record_spend(store.db, key, spent)
        cand = new or cand
    mark_round(store.db, key, started)
    if not cand:
        return "quiet"
    cur = active(store.db, key)
    room = None if daily_tokens is None else max(daily_tokens - spent_24h(store.db)[0], 0)
    if room is not None and room < 1:
        return "paused"
    base = await score(store, key, cur["version"] if cur else 0, cur["body"] if cur else None, cases, sha,
                       provider, budget, policy, workspace)
    new = await score(store, key, cand["version"], cand["body"], cases, sha, provider, budget, policy, workspace)
    line = (f"{key}: v{cand['version']} passed {new['passed']}/{new['total']} with {new['bad_calls']} mistaken calls; "
            f"current {'v' + str(cur['version']) if cur else '(none)'} {base['passed']}/{base['total']}, "
            f"{base['bad_calls']}")
    if not better(new, base):
        reject(store, key, cand["version"], "did worse in evals: " + line)
        log(line + " -> rejected")
        return "kept"
    held = needs_person(cand)
    if held:
        store.db.execute("UPDATE playbooks SET note=? WHERE id=?", (f"passed evals; needs a person: {held}", cand["id"]))
        store.db.commit()
        log(line + f" -> waiting for a person (`aios improve adopt --agent {key} {cand['version']}`): {held}")
        return "held"
    adopt(store, key, cand["version"], by="evals", note=line)
    log(line + " -> adopted (live from the next run)")
    return "adopted"


async def loop(store, keys: list[str], cases_path: str, provider, budget, policy=None, every: float = 3600,
               rounds: int = 0, use_model: bool = False, daily_tokens: int | None = DAILY_TOKENS,
               daily_cost: float | None = None, workspace=None, log=print) -> int:
    """No limit on rounds unless `rounds` > 0; rounds with nothing new are free."""
    n = 0
    while True:
        cases, sha = load_cases(cases_path)  # re-read: the person may add cases while it runs
        for key in keys:
            try:
                what = await round_(store, key, cases, sha, provider, budget, policy, use_model,
                                    daily_tokens, daily_cost, workspace, log)
                if what == "quiet":
                    log(f"{key}: nothing new since the last round")
            except Exception as e:
                log(f"{key}: round failed: {type(e).__name__}: {e}")
                _audit(store, "round", {"agent": key}, f"{type(e).__name__}: {e}", "error")
        n += 1
        if rounds and n >= rounds:
            return 0
        await asyncio.sleep(every)


# ---- patches to code or policy: filed by agents, applied only by a person ----

def file_patch(db, agent: str, path: str, diff: str, reason: str, tainted: bool) -> int:
    ensure(db)
    p = str(path).replace("\\", "/")
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in p.split("/"):
        raise ValueError("path must be relative to the AIOS folder, without '..'")
    if not diff.strip() or len(diff.encode()) > PATCH_MAX:
        raise ValueError(f"diff must be a non-empty unified diff of at most {PATCH_MAX} bytes")
    touched = patch_paths(diff)
    if not touched or any(t != p for t in touched):
        raise ValueError(f"the diff must change {p} and nothing else (it changes: {', '.join(touched) or 'nothing'})")
    cur = db.execute("INSERT INTO improve_patches(ts, agent, path, diff, reason, tainted) VALUES(?,?,?,?,?,?)",
                     (now_iso(), agent, p, diff, clean_lesson(reason)[:500], int(tainted)))
    db.commit()
    return cur.lastrowid


def patch_paths(diff: str) -> list[str]:
    out = []
    for m in re.finditer(r"^(?:\+\+\+|---) (?:[ab]/)?(\S+)", diff, re.M):
        if m[1] != "/dev/null" and m[1] not in out:
            out.append(m[1])
    return out


def patches(db, status: str | None = None) -> list[dict]:
    ensure(db)
    q, p = "SELECT id, ts, agent, path, diff, reason, tainted, status, decided_ts, detail FROM improve_patches", ()
    if status:
        q, p = q + " WHERE status=?", (status,)
    keys = ("id", "ts", "agent", "path", "diff", "reason", "tainted", "status", "decided_ts", "detail")
    return [dict(zip(keys, r)) for r in db.execute(q + " ORDER BY id", p)]


def _git_apply(root: Path, diff: str, *flags) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "apply", "--whitespace=nowarn", *flags, "-"],
                          input=diff, text=True, capture_output=True, cwd=root)


def default_check(root: Path, path: str) -> list[list[str]]:
    checks = []
    if path.endswith(".py"):
        checks.append([sys.executable, "-m", "py_compile", path])
    if path.endswith(".toml"):
        checks.append([sys.executable, "-c", "import sys,tomllib; tomllib.load(open(sys.argv[1],'rb'))", path])
    if (root / "aios.py").exists():
        checks.append([sys.executable, "-c", "import aios"])
    return checks


def approve_patch(store, pid: int, root: Path, check: str | None = None, timeout: float = 600) -> dict:
    """Apply patch `pid` (a person's decision), run the health check, and reverse it if it fails."""
    ensure(store.db)
    row = next((p for p in patches(store.db) if p["id"] == pid), None)
    if not row:
        raise ValueError(f"no patch #{pid}")
    if row["status"] != "pending":
        raise ValueError(f"patch #{pid} is {row['status']}")
    if shutil.which("git") is None:
        raise RuntimeError("applying patches needs git on PATH")
    if patch_paths(row["diff"]) != [row["path"]]:
        raise ValueError("the diff does not match its path")
    res = _git_apply(root, row["diff"], "--check")
    if res.returncode != 0:
        status, detail = "failed", "does not apply: " + (res.stderr or res.stdout).strip()[:500]
    else:
        _git_apply(root, row["diff"])
        cmds = [check] if check else default_check(root, row["path"])
        failed = None
        for cmd in cmds:
            try:
                r = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=root, capture_output=True, text=True,
                                   timeout=timeout)
                if r.returncode != 0:
                    failed = f"{cmd if isinstance(cmd, str) else ' '.join(cmd[-2:])}: " + (r.stderr or r.stdout)[-400:]
            except subprocess.TimeoutExpired:
                failed = f"health check timed out after {timeout:g}s"
            if failed:
                break
        if failed:
            _git_apply(root, row["diff"], "-R")
            status, detail = "rolled_back", "health check failed, reversed: " + failed.strip()
        else:
            status, detail = "applied", "applied; health check passed (restart running AIOS processes to load code)"
    store.db.execute("UPDATE improve_patches SET status=?, decided_ts=?, detail=? WHERE id=?",
                     (status, now_iso(), detail, pid))
    store.db.commit()
    _audit(store, "patch", {"id": pid, "path": row["path"], "by": "person"}, detail,
           "ok" if status == "applied" else "error")
    return {**row, "status": status, "detail": detail}


def reject_patch(store, pid: int, why: str = "") -> bool:
    ensure(store.db)
    n = store.db.execute("UPDATE improve_patches SET status='rejected', decided_ts=?, detail=? WHERE id=? "
                         "AND status='pending'", (now_iso(), why, pid)).rowcount
    store.db.commit()
    if n:
        _audit(store, "patch", {"id": pid, "by": "person"}, "rejected " + why)
    return bool(n)


# ---- agent tools ----

def install_tools(k):
    """suggest_lesson and propose_patch. Neither is granted by default (list them in permissions).
    Neither changes anything that runs: one adds to a candidate playbook, the other files a patch."""

    @k.tools.register("suggest_lesson", "Suggest one lesson for your own playbook (how to do your tasks better "
                      "next time). It is tested on eval tasks before it is used.",
                      {"lesson": {"type": "string", "description": "one short, concrete sentence"}},
                      required=["lesson"], ctx=True)
    async def suggest_lesson(lesson: str, agent):
        text = clean_lesson(lesson)
        if len(text) < 8:
            return "too short: write one concrete sentence"
        tainted = k._is_tainted(agent)
        key = agent.rate_key or agent.name
        cand = add_candidate(k.store.db, key, [text], "agent", tainted)
        if not cand:
            return "already in your playbook"
        held = needs_person(cand)
        return (f"added to candidate playbook v{cand['version']}; it is tried on eval tasks before use"
                + (f" and needs a person because {held}" if held else ""))

    @k.tools.register("propose_patch", "File a change to an AIOS code or policy file for a person to review. "
                      "It is never applied by itself.",
                      {"path": {"type": "string", "description": "file relative to the AIOS folder"},
                       "diff": {"type": "string", "description": "unified diff (git diff format) of that file"},
                       "reason": {"type": "string"}},
                      required=["path", "diff", "reason"], ctx=True)
    async def propose_patch(path: str, diff: str, reason: str, agent):
        pid = file_patch(k.store.db, agent.name, path, diff, reason, k._is_tainted(agent))
        return f"filed as patch #{pid}; a person reviews it with `aios improve patches`"


# ---- CLI ----

def add_parser(sub, add_model_flags):
    p = sub.add_parser("improve", help="self-improvement: versioned playbooks learned from runs, tested before use")
    p.add_argument("action", choices=["list", "show", "propose", "eval", "adopt", "reject", "rollback", "check",
                                      "run", "loop", "patches", "approve-patch", "reject-patch"])
    p.add_argument("version", nargs="?", type=int, help="adopt/reject/show/eval: playbook version; "
                   "approve-patch/reject-patch: patch number")
    p.add_argument("--agent", action="append", help="policy agent (loop: repeat for several)")
    p.add_argument("--cases", metavar="FILE", help='eval tasks, one per line: text or {"task", "expect", "forbid"} '
                   "(regular expressions on the answer)")
    p.add_argument("--policy", help="TOML policy (permissions the agent has during evals)")
    p.add_argument("--home", default=".", help="folder holding aios.db and workspace/ (default: current)")
    p.add_argument("--use-model", action="store_true", help="propose: also let the model write lessons from the counts")
    p.add_argument("--since", help="propose: look at the audit log from this ISO time (default: last round)")
    p.add_argument("--to", type=int, help="rollback: this version (0 = no playbook)")
    p.add_argument("--every", default="1h", help="loop: time between rounds (s, min, hour; default 1h)")
    p.add_argument("--rounds", type=int, default=0, help="loop: stop after N rounds (default 0 = never)")
    p.add_argument("--daily-tokens", type=int, default=DAILY_TOKENS,
                   help=f"run/loop: eval tokens per 24h before pausing (default {DAILY_TOKENS:,}; 0 = no cap)")
    p.add_argument("--daily-cost", type=float, help="run/loop: eval USD per 24h before pausing (needs --price-in/out)")
    p.add_argument("--check", help="approve-patch: health check command (default: compile/parse it, import aios)")
    p.add_argument("--root", help="approve-patch: folder the patch applies to (default: the AIOS folder)")
    p.add_argument("--why", default="", help="reject/rollback/reject-patch: reason, kept in the record")
    add_model_flags(p)
    return p


def _secs(text: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|sec|min|m|h|hour|d|day)?", text.strip())
    if not m:
        raise ValueError(f"bad interval {text!r} (e.g. 30min, 1h)")
    return float(m[1]) * {"s": 1, "sec": 1, None: 1, "min": 60, "m": 60, "h": 3600, "hour": 3600,
                          "d": 86400, "day": 86400}[m[2]]


def cli(a, ap, build_model, load_policy) -> int:
    import aios
    base = Path(a.home).resolve()
    aios.WORKSPACE = (base / "workspace").resolve()
    store = aios.Store(str(base / "aios.db"))
    ensure(store.db)
    keys = a.agent or []
    one = lambda: keys[0] if len(keys) == 1 else ap.error(f"improve {a.action} needs exactly one --agent")
    try:
        if a.action == "list":
            rows = versions(store.db, keys[0] if keys else None)
            for r in rows:
                mark = {"active": "*", "candidate": "?"}.get(r["status"], " ")
                print(f"{mark} {r['key']} v{r['version']} {r['status']:<11} {r['source']:<12} "
                      f"{len(lessons(r['body']))} lessons{'  [tainted]' if r['tainted'] else ''}"
                      f"{'  [flagged]' if r['flagged'] else ''}  {r['note'][:80]}")
            if not rows:
                print("no playbooks yet (aios improve propose --agent NAME)")
            t, c = spent_24h(store.db)
            print(f"eval spending, last 24h: {t} tokens, ${c:.4f}; patches waiting: {len(patches(store.db, 'pending'))}")
            return 0
        if a.action == "show":
            key = one()
            pb = get(store.db, key, a.version) if a.version else (active(store.db, key) or open_candidate(store.db, key))
            if not pb:
                print(f"{key}: no playbook")
                return 1
            print(f"{key} v{pb['version']} ({pb['status']}, from {pb['source']}, {pb['ts']})\n{pb['body']}")
            if needs_person(pb):
                print(f"needs a person: {needs_person(pb)}")
            return 0
        if a.action in ("adopt", "reject"):
            key = one()
            if not a.version:
                ap.error(f"improve {a.action} needs a version")
            if a.action == "reject":
                return 0 if reject(store, key, a.version, a.why) else 1
            pb = adopt(store, key, a.version, by="person")
            print(f"{key}: v{pb['version']} is live from the next run")
            return 0
        if a.action == "rollback":
            pb = rollback(store, one(), a.to, a.why or "by a person")
            print(f"{keys[0]}: now {'v' + str(pb['version']) if pb else 'no playbook'}")
            return 0
        if a.action == "check":
            for key in keys or sorted({r["key"] for r in versions(store.db)}):
                h = health(store, key)
                print(f"{key}: " + ("not enough runs to judge yet" if not h else
                                    f"v{h['version']} {h['after']:.0%} failed runs over {h['runs']}, "
                                    f"{h['before']:.0%} before" + (" -> ROLLED BACK" if h.get("rolled_back") else "")))
            return 0
        if a.action == "patches":
            rows = patches(store.db)
            for r in rows:
                print(f"#{r['id']} {r['ts']} {r['status']:<11} {r['agent']} -> {r['path']}"
                      f"{'  [tainted]' if r['tainted'] else ''}: {r['reason']}")
                if r["status"] == "pending":
                    print("  " + r["diff"].strip().replace("\n", "\n  "))
                elif r["detail"]:
                    print(f"  {r['detail']}")
            if not rows:
                print("no patches filed")
            return 0
        if a.action in ("approve-patch", "reject-patch"):
            if not a.version:
                ap.error(f"improve {a.action} needs a patch number")
            if a.action == "reject-patch":
                return 0 if reject_patch(store, a.version, a.why) else 1
            root = Path(a.root).resolve() if a.root else Path(aios.__file__).resolve().parent
            r = approve_patch(store, a.version, root, a.check)
            print(f"patch #{r['id']}: {r['status']}: {r['detail']}")
            return 0 if r["status"] == "applied" else 1

        # these need a model
        provider, budget = build_model(a, ap, default_tokens=20_000)
        policy = None
        if a.policy:
            try:
                policy = load_policy(a.policy)
            except (OSError, ValueError) as e:
                ap.error(f"--policy {a.policy}: {e}")
        if not is_free(provider) and a.daily_cost is None and (a.action == "eval" or a.use_model):
            ap.error("that would call a paid model: use a local one (--provider openai --model ...) or set a "
                     "USD cap with --daily-cost and --price-in/--price-out")
        if a.action == "propose":
            cand, sig, spent = asyncio.run(propose(store, one(), a.since, provider if a.use_model else None))
            record_spend(store.db, keys[0], spent)
            print(f"{keys[0]}: {sig['runs']} runs, {sum(sig['calls'].values())} mistaken calls, "
                  f"{sum(sig['stops'].values())} runs not finished")
            print(f"candidate v{cand['version']}:\n{cand['body']}" if cand else "nothing new to learn")
            return 0
        if not a.cases:
            ap.error(f"improve {a.action} needs --cases FILE")
        try:
            cases, sha = load_cases(a.cases)
        except (OSError, ValueError, re.error) as e:
            ap.error(f"--cases: {e}")
        daily = a.daily_tokens or None
        if a.daily_cost is not None and not (a.price_in or a.price_out):
            ap.error("--daily-cost needs --price-in / --price-out")
        if a.action == "eval":
            key = one()
            cand = get(store.db, key, a.version) if a.version else open_candidate(store.db, key)
            cur = active(store.db, key)
            for label, pb in (("current", cur), ("candidate", cand)):
                if label == "candidate" and not cand:
                    print("no candidate to compare")
                    break
                r = asyncio.run(score(store, key, pb["version"] if pb else 0, pb["body"] if pb else None,
                                      cases, sha, provider, budget, policy))
                print(f"{label} {'v' + str(pb['version']) if pb else '(none)'}: {r['passed']}/{r['total']} passed, "
                      f"{r['bad_calls']} mistaken calls, {r['tokens']} tokens{' (cached)' if r.get('cached') else ''}")
            return 0
        if a.action == "run":
            what = asyncio.run(round_(store, one(), cases, sha, provider, budget, policy, a.use_model,
                                      daily, a.daily_cost))
            print(f"{keys[0]}: {what}")
            return 0
        if a.action == "loop":
            if not keys:
                ap.error("improve loop needs --agent (repeat it for several agents)")
            try:
                every = _secs(a.every)
            except ValueError as e:
                ap.error(str(e))
            print(f"self-improvement loop for {', '.join(keys)}: a round every {a.every}, "
                  f"{'no limit on rounds' if not a.rounds else f'{a.rounds} rounds'}, eval cap "
                  f"{daily or 'none'} tokens/24h. Ctrl+C stops it.", flush=True)
            try:
                return asyncio.run(loop(store, keys, a.cases, provider, budget, policy, every, a.rounds,
                                        a.use_model, daily, a.daily_cost,
                                        log=lambda s: print(f"[{now_iso()}] {s}", flush=True)))
            except KeyboardInterrupt:
                return 0
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        store.db.close()
    return 0
