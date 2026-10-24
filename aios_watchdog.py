"""Watchdog and heartbeat for aios-core: the system agents that keep unattended runs honest.

    python aios.py status --home ~/aios-data        # is the daemon alive? open alerts, waiting approvals
    python aios.py alerts --home ~/aios-data        # open alerts; --all, --ack ID|all, --check

The daemon writes a heartbeat and runs these rules on every tick. No model is involved on purpose: a
model reading the audit log would read attacker-written text, and a watchdog that can be talked out
of an alert is no watchdog. The rules only read the database and add alerts; they never act.

Rules (over the last WINDOW_MINUTES):
  tainted-refused   a risky call was refused because the run had read untrusted content: the
                    classic shape of a prompt injection trying to act
  unattended-refused  a scheduled job tried a risky call its policy does not pre-allow
  refusal-spike     one agent was refused DENIED_SPIKE or more times
  budget-stop       a run stopped on its token or cost budget
  job-failing       a job's last JOB_FAIL_RUNS runs all ended in error or timeout
The same rule for the same subject is not raised again within COOLDOWN_MINUTES.
"""
import sqlite3
from datetime import datetime, timedelta

WINDOW_MINUTES = 15
COOLDOWN_MINUTES = 60
DENIED_SPIKE = 10
JOB_FAIL_RUNS = 3
STALE_AFTER = 120  # seconds without a heartbeat before the daemon counts as down (at least 3 ticks)
RUN_ENTRY = "(run)"
SEVERITY = {"tainted-refused": "critical", "unattended-refused": "warning", "refusal-spike": "warning",
            "budget-stop": "info", "job-failing": "warning"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, rule TEXT, severity TEXT, subject TEXT,
    detail TEXT, acked_ts TEXT);
CREATE TABLE IF NOT EXISTS heartbeat(name TEXT PRIMARY KEY, ts TEXT, info TEXT);
"""

iso = lambda d: d.isoformat(timespec="seconds")


def ensure(db: sqlite3.Connection):
    db.executescript(SCHEMA)


def beat(db: sqlite3.Connection, name: str = "daemon", info: str = "", now=None):
    ensure(db)
    db.execute("INSERT OR REPLACE INTO heartbeat(name, ts, info) VALUES(?,?,?)",
               (name, iso(now or datetime.now()), info))
    db.commit()


def liveness(db: sqlite3.Connection, name: str = "daemon", now=None, stale_after: float = STALE_AFTER) -> dict:
    """{'state': 'never' | 'alive' | 'down', 'ts', 'age', 'info'}"""
    ensure(db)
    row = db.execute("SELECT ts, info FROM heartbeat WHERE name=?", (name,)).fetchone()
    if not row:
        return {"state": "never", "ts": None, "age": None, "info": ""}
    age = ((now or datetime.now()) - datetime.fromisoformat(row[0])).total_seconds()
    return {"state": "alive" if age <= stale_after else "down", "ts": row[0], "age": age, "info": row[1]}


def _raise(db, now, rule: str, subject: str, detail: str) -> dict | None:
    since = iso(now - timedelta(minutes=COOLDOWN_MINUTES))
    if db.execute("SELECT 1 FROM alerts WHERE rule=? AND subject=? AND ts>=?", (rule, subject, since)).fetchone():
        return None
    a = {"ts": iso(now), "rule": rule, "severity": SEVERITY[rule], "subject": subject, "detail": detail[:500]}
    a["id"] = db.execute("INSERT INTO alerts(ts, rule, severity, subject, detail) VALUES(?,?,?,?,?)",
                         tuple(a.values())).lastrowid
    return a


def check(db: sqlite3.Connection, now=None) -> list[dict]:
    """Run every rule once; returns the alerts it added."""
    ensure(db)
    now = now or datetime.now()
    since = iso(now - timedelta(minutes=WINDOW_MINUTES))
    found: list[tuple[str, str, str]] = []
    denied = db.execute("SELECT agent, tool, result FROM audit WHERE status='denied' AND ts>=? AND tool!=? "
                        "ORDER BY id", (since, RUN_ENTRY)).fetchall()
    counts: dict[str, int] = {}
    for agent, tool, result in denied:
        counts[agent] = counts.get(agent, 0) + 1
        result = result or ""
        if "read untrusted content" in result:
            found.append(("tainted-refused", agent,
                          f"{agent} tried {tool} after reading untrusted content; it was refused. "
                          "Look at what the run read (aios audit) before widening its policy."))
        elif agent.startswith("job-") and result.startswith("not approved by a human"):
            found.append(("unattended-refused", agent,
                          f"{agent} tried {tool}, which its policy does not pre-allow; it was refused."))
    for agent, n in counts.items():
        if n >= DENIED_SPIKE:
            found.append(("refusal-spike", agent, f"{agent} was refused {n} times in {WINDOW_MINUTES} minutes."))
    for agent, result in db.execute("SELECT agent, result FROM audit WHERE tool=? AND status='budget' AND ts>=?",
                                    (RUN_ENTRY, since)).fetchall():
        found.append(("budget-stop", agent, f"a run of {agent} stopped on its budget ({result})."))
    jobs = [j for (j,) in db.execute("SELECT DISTINCT job FROM job_runs").fetchall()]
    for job in jobs:
        last = db.execute("SELECT status FROM job_runs WHERE job=? AND status!='skipped' ORDER BY id DESC LIMIT ?",
                          (job, JOB_FAIL_RUNS)).fetchall()
        if len(last) == JOB_FAIL_RUNS and all(s in ("error", "timeout") for (s,) in last):
            found.append(("job-failing", f"job-{job}",
                          f"job {job} failed its last {JOB_FAIL_RUNS} runs (aios jobs history {job})."))
    added = [a for rule, subject, detail in found if (a := _raise(db, now, rule, subject, detail))]
    db.commit()
    return added


def list_alerts(db: sqlite3.Connection, include_acked: bool = False, n: int = 50) -> list[dict]:
    ensure(db)
    q = "SELECT id, ts, rule, severity, subject, detail, acked_ts FROM alerts"
    q += "" if include_acked else " WHERE acked_ts IS NULL"
    rows = db.execute(q + " ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    keys = ("id", "ts", "rule", "severity", "subject", "detail", "acked_ts")
    return [dict(zip(keys, r)) for r in rows]


def ack(db: sqlite3.Connection, target: str, now=None) -> int:
    """target: an alert id or 'all'. Returns how many were acknowledged."""
    ensure(db)
    ts = iso(now or datetime.now())
    if target == "all":
        n = db.execute("UPDATE alerts SET acked_ts=? WHERE acked_ts IS NULL", (ts,)).rowcount
    else:
        n = db.execute("UPDATE alerts SET acked_ts=? WHERE id=? AND acked_ts IS NULL", (ts, int(target))).rowcount
    db.commit()
    return n
