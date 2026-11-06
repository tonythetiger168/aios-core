"""Supervisor: AIOS watches itself and acts on what the watchdog finds, without a person and without a model.

    python aios.py supervisor --home ~/aios-data             # what it did (pauses, holds), newest first
    python aios.py supervisor --release ID|all --home ...    # a person undoes one action (or all)
    python aios.py daemon ... --no-supervise                 # alerts only, as before

The watchdog (aios_watchdog) only raises alerts. On every daemon tick the supervisor reads the new
alerts and answers each with one fixed, rule-based action:

  pause-job    job-failing, budget-runaway, and tainted-refused / unattended-refused / loop-stop /
               refusal-spike when the subject is a job: the job is disabled (enabled=0)
  hold-agent   the same refusal rules for any other agent (chat, a2a guest, swarm helper): every risky
               call of that agent needs a person, interactively, and auto-approving scopes stop
               applying, until a person releases it. Tools that need no approval keep working
  safe-mode    audit-tampered: every job is paused and every agent is held ('*')
  notify       everything else (budget-stop, audit-sink-failing, rules it does not know)

Every action is written to the audit log (agent 'supervisor') and to supervisor_actions, and the
Telegram bridge (`aios telegram`) sends it to your phone as plain text.

What the supervisor can never do, by construction:
- Anything that adds risk. Its only writes are: disable a job, add a hold, record an action. It
  never enables a job, removes a hold, grants a permission, changes a policy or scope, answers an
  approval, or edits / deletes an audit entry. Undoing is release(), which only the `aios supervisor
  --release` command calls: a person, never the daemon, never an agent (no agent tool reaches it).
- Spend money. No model is involved: a model reading the audit log would read attacker-written
  text, and a supervisor that can be talked out of a pause is no supervisor.
- Be turned off by an agent: only the person starting the daemon can (--no-supervise).
"""
import json
import sqlite3
from datetime import datetime, timedelta

ACTIONS = ("pause-job", "hold-agent", "safe-mode", "notify")  # all of them only reduce risk
REFUSAL_RULES = ("tainted-refused", "unattended-refused", "loop-stop", "refusal-spike")
BUDGET_RUNAWAY = 3  # a job stopped on its budget this many times in a day is paused
BUDGET_WINDOW_HOURS = 24
FIRST_LOOK_MINUTES = 60  # on a database it never saw, act on alerts from the last hour only
ALL = "*"
AGENT = "supervisor"
TOOL = "(supervisor)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS supervisor_actions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, alert_id INTEGER, rule TEXT, subject TEXT,
    action TEXT, target TEXT, detail TEXT, released_ts TEXT);
CREATE TABLE IF NOT EXISTS holds(agent TEXT PRIMARY KEY, ts TEXT, reason TEXT, action_id INTEGER);
CREATE TABLE IF NOT EXISTS supervisor_state(key TEXT PRIMARY KEY, value TEXT);
"""

iso = lambda d: d.isoformat(timespec="seconds")


def ensure(db: sqlite3.Connection):
    db.executescript(SCHEMA)


def held(db: sqlite3.Connection, *names: str) -> str | None:
    """The reason one of these agents (or everyone) is on hold, else None. Cheap: one indexed lookup."""
    keys = [n for n in names if n] + [ALL]
    try:
        r = db.execute(f"SELECT reason FROM holds WHERE agent IN ({','.join('?' * len(keys))}) LIMIT 1",
                       keys).fetchone()
    except sqlite3.OperationalError:  # an older database without the table: nobody is held
        return None
    return r[0] if r else None


def _job_name(db, subject: str) -> str | None:
    if not subject.startswith("job-"):
        return None
    name = subject[4:]
    return name if db.execute("SELECT 1 FROM jobs WHERE name=?", (name,)).fetchone() else None


def _audit(db, action: str, target: str, detail: str):
    import aios  # the one audit writer, so the hash chain stays whole
    rows = [aios.Store.audit_row(AGENT, TOOL, {"action": action, "target": target}, detail, "ok")]
    aios.Store.append_audit(_Shim(db), rows)


class _Shim:  # lets Store.append_audit run on a bare connection
    def __init__(self, db):
        self.db = db


def _record(db, now, alert: dict | None, action: str, target: str, detail: str) -> dict:
    assert action in ACTIONS
    rule, subject = (alert or {}).get("rule", ""), (alert or {}).get("subject", "")
    a = {"ts": iso(now), "alert_id": (alert or {}).get("id"), "rule": rule, "subject": subject,
         "action": action, "target": target, "detail": detail[:500]}
    a["id"] = db.execute("INSERT INTO supervisor_actions(ts, alert_id, rule, subject, action, target, detail) "
                         "VALUES(?,?,?,?,?,?,?)", tuple(a.values())).lastrowid
    db.commit()
    _audit(db, action, target, detail[:500])
    return a


def _pause(db, now, alert, job: str, why: str) -> dict | None:
    if not db.execute("UPDATE jobs SET enabled=0 WHERE name=? AND enabled=1", (job,)).rowcount:
        return None  # already off (by a person, by the schedule, or by us): nothing to do
    return _record(db, now, alert, "pause-job", job,
                   f"paused job {job}: {why} Resume it with `aios supervisor --release ID` once you have looked.")


def _hold(db, now, alert, agent: str, why: str, action: str = "hold-agent") -> dict | None:
    if db.execute("SELECT 1 FROM holds WHERE agent=?", (agent,)).fetchone():
        return None
    who = "every agent" if agent == ALL else f"agent {agent}"
    a = _record(db, now, alert, action, agent,
                f"{who} on hold: every risky call needs a person until released. {why}")
    db.execute("INSERT OR IGNORE INTO holds(agent, ts, reason, action_id) VALUES(?,?,?,?)",
               (agent, iso(now), why[:300], a["id"]))
    db.commit()
    return a


def respond(db: sqlite3.Connection, alert: dict, now=None) -> list[dict]:
    """The fixed answer to one alert. Returns the actions taken (empty if there was nothing to do)."""
    ensure(db)
    now = now or datetime.now()
    rule, subject = alert["rule"], alert["subject"]
    why = f"[{rule}] {alert.get('detail', '')}"
    if subject == AGENT:  # never act on itself in a loop
        return []
    out: list[dict | None] = []
    if rule == "audit-tampered":
        jobs = [j for (j,) in db.execute("SELECT name FROM jobs WHERE enabled=1 ORDER BY name").fetchall()]
        out += [_pause(db, now, alert, j, "the audit log no longer checks out (safe mode).") for j in jobs]
        out.append(_hold(db, now, alert, ALL, why, action="safe-mode"))
    elif rule in ("job-failing", "budget-runaway") or rule in REFUSAL_RULES:
        job = _job_name(db, subject)
        if job:
            out.append(_pause(db, now, alert, job, why))
        elif rule in REFUSAL_RULES:
            out.append(_hold(db, now, alert, subject, why))
    else:
        out.append(_record(db, now, alert, "notify", subject, why))
    return [a for a in out if a]


def own_rules(db: sqlite3.Connection, now=None) -> list[dict]:
    """Rules of the supervisor's own, raised as watchdog alerts: a job that keeps hitting its budget."""
    import aios_watchdog as wd
    now = now or datetime.now()
    since = iso(now - timedelta(hours=BUDGET_WINDOW_HOURS))
    found = []
    for agent, n in db.execute("SELECT agent, COUNT(*) FROM audit WHERE tool=? AND status='budget' AND ts>=? "
                               "AND agent LIKE 'job-%' GROUP BY agent", (wd.RUN_ENTRY, since)).fetchall():
        if n >= BUDGET_RUNAWAY:
            a = wd.alert(db, "budget-runaway", agent, f"{agent} stopped on its budget {n} times in "
                         f"{BUDGET_WINDOW_HOURS} hours: it spends without finishing.", now=now)
            if a:
                found.append(a)
    return found


def tick(db: sqlite3.Connection, now=None) -> list[dict]:
    """Answer every alert raised since the last tick (the daemon calls this after the watchdog)."""
    import aios_watchdog as wd
    ensure(db)
    wd.ensure(db)
    now = now or datetime.now()
    own_rules(db, now)
    r = db.execute("SELECT value FROM supervisor_state WHERE key='last_alert'").fetchone()
    if r is None:  # first look at this database: old alerts were for a person, not for us
        old = db.execute("SELECT MAX(id) FROM alerts WHERE ts<?",
                         (iso(now - timedelta(minutes=FIRST_LOOK_MINUTES)),)).fetchone()[0]
        last = old or 0
    else:
        last = int(r[0])
    keys = ("id", "ts", "rule", "severity", "subject", "detail")
    rows = db.execute("SELECT id, ts, rule, severity, subject, detail FROM alerts WHERE id>? AND acked_ts IS NULL "
                      "ORDER BY id", (last,)).fetchall()
    done = []
    for row in rows:
        done += respond(db, dict(zip(keys, row)), now)
    top = db.execute("SELECT MAX(id) FROM alerts").fetchone()[0] or last
    db.execute("INSERT OR REPLACE INTO supervisor_state(key, value) VALUES('last_alert', ?)", (str(max(top, last)),))
    db.commit()
    return done


def actions(db: sqlite3.Connection, include_released: bool = True, n: int = 50, after: int = 0) -> list[dict]:
    ensure(db)
    q = "SELECT id, ts, rule, subject, action, target, detail, released_ts FROM supervisor_actions WHERE id>?"
    q += "" if include_released else " AND released_ts IS NULL"
    keys = ("id", "ts", "rule", "subject", "action", "target", "detail", "released_ts")
    return [dict(zip(keys, r)) for r in db.execute(q + " ORDER BY id DESC LIMIT ?", (after, n)).fetchall()]


def release(db: sqlite3.Connection, target: str, now=None) -> list[str]:
    """A PERSON undoes supervisor actions: an action id, or 'all'. Only `aios supervisor --release`
    calls this. A paused job is enabled again only if it is still off; a hold is lifted."""
    ensure(db)
    now = now or datetime.now()
    q = "SELECT id, action, target FROM supervisor_actions WHERE released_ts IS NULL AND action!='notify'"
    rows = db.execute(q + ("" if target == "all" else " AND id=?"), () if target == "all" else (int(target),)).fetchall()
    out = []
    for aid, action, tgt in rows:
        if action == "pause-job":
            if db.execute("UPDATE jobs SET enabled=1 WHERE name=? AND enabled=0", (tgt,)).rowcount:
                out.append(f"#{aid}: job {tgt} resumed")
        else:
            if db.execute("DELETE FROM holds WHERE agent=? AND action_id=?", (tgt, aid)).rowcount:
                out.append(f"#{aid}: {'every agent' if tgt == ALL else tgt} released")
        db.execute("UPDATE supervisor_actions SET released_ts=? WHERE id=?", (iso(now), aid))
    db.commit()
    import aios
    if rows:
        aios.Store.append_audit(_Shim(db), [aios.Store.audit_row(
            "human", TOOL, {"release": target}, "; ".join(out) or "nothing left to undo", "ok")])
    return out


def summary(db: sqlite3.Connection) -> dict:
    ensure(db)
    paused = db.execute("SELECT COUNT(*) FROM supervisor_actions a JOIN jobs j ON j.name=a.target "
                        "WHERE a.action='pause-job' AND a.released_ts IS NULL AND j.enabled=0").fetchone()[0]
    holds = [r[0] for r in db.execute("SELECT agent FROM holds ORDER BY agent").fetchall()]
    return {"paused_jobs": paused, "holds": holds}


def text(a: dict) -> str:
    """Plain text for a phone notification (no Markdown: the detail can hold attacker-chosen names)."""
    return f"AIOS supervisor #{a['id']}: {a['action']} {a['target']}\n{a['detail']}"


def as_json(a: dict) -> str:
    return json.dumps(a, ensure_ascii=False)
