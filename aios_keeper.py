"""Memory keeper for aios-core: review, confirm and tidy what agents have remembered.

    python aios.py memory review [--agent NAME]          # notes saved while a run had read untrusted content
    python aios.py memory confirm AGENT KEY               # you vouch for one: it stops being "unverified"
    python aios.py memory tidy [--days 30] [--dry-run]    # archive old unverified notes, drop stale vectors
    python aios.py memory archived | restore AGENT KEY    # nothing tidy does is lost

Deterministic, no model: deciding what an agent should keep believing is exactly what an injected
note would like to influence. Tidy never deletes a note; it moves it to an archive you can restore
from. Only cached vectors (recomputable) are deleted.
"""
import hashlib
from datetime import datetime, timedelta

TAINTED_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts_archive(
    agent TEXT, key TEXT, value TEXT, source TEXT, ts TEXT, archived_ts TEXT, reason TEXT,
    PRIMARY KEY(agent, key, archived_ts));
"""

iso = lambda d: d.isoformat(timespec="seconds")


def ensure(db):
    db.executescript(SCHEMA)


def review(db, agent: str | None = None) -> list[dict]:
    q, params = "SELECT agent, key, value, ts FROM facts WHERE source='tainted'", []
    if agent:
        q += " AND agent=?"
        params.append(agent)
    rows = db.execute(q + " ORDER BY ts", params).fetchall()
    return [dict(zip(("agent", "key", "value", "ts"), r)) for r in rows]


def confirm(db, agent: str, key: str) -> bool:
    """The person read the note and vouches for it: it becomes theirs ('user'), so the model can no
    longer overwrite it and it is no longer shown as unverified."""
    n = db.execute("UPDATE facts SET source='user' WHERE agent=? AND key=? AND source='tainted'",
                   (agent, key)).rowcount
    db.commit()
    return n == 1


def _archive(db, agent: str, key: str, reason: str, now) -> bool:
    row = db.execute("SELECT value, source, ts FROM facts WHERE agent=? AND key=?", (agent, key)).fetchone()
    if not row:
        return False
    db.execute("INSERT INTO facts_archive(agent, key, value, source, ts, archived_ts, reason) VALUES(?,?,?,?,?,?,?)",
               (agent, key, *row, iso(now), reason))
    db.execute("DELETE FROM facts WHERE agent=? AND key=?", (agent, key))
    db.execute("DELETE FROM fact_vecs WHERE agent=? AND key=?", (agent, key))
    return True


def _vec_sha(key: str, value: str) -> str:  # same text and hash as aios_memory._vector_rank
    return hashlib.sha256(f"{key}: {value}".encode("utf-8")).hexdigest()[:16]


def tidy(db, days: int = TAINTED_DAYS, dry_run: bool = False, now=None) -> dict:
    """{'archived': [(agent, key)], 'stale_vectors': n, 'duplicates': [(agent, value, [keys])]}"""
    ensure(db)
    now = now or datetime.now()
    cutoff = iso(now - timedelta(days=days))
    old = db.execute("SELECT agent, key FROM facts WHERE source='tainted' AND (ts IS NULL OR ts<?) "
                     "ORDER BY agent, key", (cutoff,)).fetchall()
    facts = {(a, k): v for a, k, v in db.execute("SELECT agent, key, value FROM facts").fetchall()}
    stale = [(a, k, m) for a, k, m, sha in db.execute("SELECT agent, key, model, sha FROM fact_vecs").fetchall()
             if (a, k) not in facts or sha != _vec_sha(k, facts[(a, k)])]
    groups: dict[tuple[str, str], list[str]] = {}
    for (a, k), v in facts.items():
        norm = " ".join(str(v).split()).lower()
        if norm:
            groups.setdefault((a, norm), []).append(k)
    dups = [(a, v, sorted(ks)) for (a, v), ks in sorted(groups.items()) if len(ks) > 1]
    if not dry_run:
        for a, k in old:
            _archive(db, a, k, f"unverified for over {days} days", now)
        for a, k, m in stale:
            db.execute("DELETE FROM fact_vecs WHERE agent=? AND key=? AND model=?", (a, k, m))
        db.commit()
    return {"archived": [tuple(r) for r in old], "stale_vectors": len(stale), "duplicates": dups}


def archived(db, agent: str | None = None, n: int = 50) -> list[dict]:
    ensure(db)
    q, params = "SELECT agent, key, value, source, archived_ts, reason FROM facts_archive", []
    if agent:
        q += " WHERE agent=?"
        params.append(agent)
    rows = db.execute(q + " ORDER BY archived_ts DESC LIMIT ?", (*params, n)).fetchall()
    return [dict(zip(("agent", "key", "value", "source", "archived_ts", "reason"), r)) for r in rows]


def restore(db, agent: str, key: str) -> str:
    """Put the latest archived copy back, with its original source. 'restored' | 'exists' | 'missing'"""
    ensure(db)
    row = db.execute("SELECT value, source, ts, archived_ts FROM facts_archive WHERE agent=? AND key=? "
                     "ORDER BY archived_ts DESC LIMIT 1", (agent, key)).fetchone()
    if not row:
        return "missing"
    if db.execute("SELECT 1 FROM facts WHERE agent=? AND key=?", (agent, key)).fetchone():
        return "exists"  # never overwrite a note written since
    db.execute("INSERT INTO facts(agent, key, value, source, ts) VALUES(?,?,?,?,?)", (agent, key, *row[:3]))
    db.execute("DELETE FROM facts_archive WHERE agent=? AND key=? AND archived_ts=?", (agent, key, row[3]))
    db.commit()
    return "restored"
