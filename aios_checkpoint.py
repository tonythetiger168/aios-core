"""Workspace checkpoints for aios-core: put back the files an agent's run changed.

    python aios.py undo --list [--agent NAME]               # recent runs that changed workspace files
    python aios.py undo [--agent NAME | --run ID] [--dry-run] [--force]

Before write_file creates or overwrites a file, what was there (or the fact that nothing was) is saved
in aios.db. Undoing a run puts every file it touched back the way it was before the run: overwritten
files get their old content back, created files are removed. A file changed again after the run (by
you or a later run) is skipped unless --force, so undo never silently throws away newer work; undo the
later run first. Not covered: run_python (it writes only its own temporary folder) and MCP tools (they
write wherever their server can). Versions older than KEEP_DAYS are dropped.
"""
import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path

KEEP_DAYS = 30
MAX_BYTES = 1_000_000  # a bigger previous file is not kept (undo then skips it and says so)
NEW, SAVED, TOO_LARGE = 0, 1, 2  # file_versions.existed: what was there before the write

SCHEMA = """
CREATE TABLE IF NOT EXISTS file_versions(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, agent TEXT, run TEXT, path TEXT,
    existed INTEGER, content BLOB, after_sha TEXT, undone_ts TEXT);
CREATE INDEX IF NOT EXISTS file_versions_run ON file_versions(run);
"""

iso = lambda d: d.isoformat(timespec="seconds")
sha = lambda b: hashlib.sha256(b).hexdigest()


def ensure(db):
    db.executescript(SCHEMA)


def new_run_id(now=None) -> str:
    return f"{(now or datetime.now()):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def record(db, agent: str, run: str, root: Path, path: Path, new: bytes, now=None):
    """Call before writing `new` to `path` (inside the workspace `root`)."""
    ensure(db)
    now = now or datetime.now()
    if path.is_file():
        size = path.stat().st_size
        existed, old = (SAVED, path.read_bytes()) if size <= MAX_BYTES else (TOO_LARGE, None)
    else:
        existed, old = NEW, None
    db.execute("INSERT INTO file_versions(ts, agent, run, path, existed, content, after_sha) VALUES(?,?,?,?,?,?,?)",
               (iso(now), agent, run, path.relative_to(root).as_posix(), existed, old, sha(new)))
    db.execute("DELETE FROM file_versions WHERE ts < ?", (iso(now - timedelta(days=KEEP_DAYS)),))
    db.commit()


def runs(db, agent: str | None = None, limit: int = 20) -> list[dict]:
    """Newest first: runs that changed files, with the files and whether they are undone."""
    ensure(db)
    q = ("SELECT run, agent, MIN(ts), MAX(ts), COUNT(undone_ts) = COUNT(*) FROM file_versions "
         + ("WHERE agent=? " if agent else "") + "GROUP BY run ORDER BY MAX(id) DESC LIMIT ?")
    out = []
    for run, ag, first, last, undone in db.execute(q, ((agent,) if agent else ()) + (limit,)).fetchall():
        paths = [p for (p,) in db.execute("SELECT DISTINCT path FROM file_versions WHERE run=? ORDER BY path",
                                          (run,))]
        out.append({"run": run, "agent": ag, "first": first, "last": last, "undone": bool(undone),
                    "paths": paths})
    return out


def latest(db, agent: str | None = None) -> str | None:
    """The newest run with changes not undone yet."""
    ensure(db)
    q = "SELECT run FROM file_versions WHERE undone_ts IS NULL" + (" AND agent=?" if agent else "")
    row = db.execute(q + " ORDER BY id DESC LIMIT 1", (agent,) if agent else ()).fetchone()
    return row[0] if row else None


def undo(db, root: Path, run: str, force: bool = False, dry_run: bool = False, now=None) -> list[tuple[str, str]]:
    """Put back each file `run` changed; returns (path, what happened) per file."""
    ensure(db)
    root = root.resolve()
    rows = db.execute("SELECT id, path, existed, content, after_sha FROM file_versions "
                      "WHERE run=? AND undone_ts IS NULL ORDER BY id", (run,)).fetchall()
    by_path: dict[str, list] = {}
    for r in rows:
        by_path.setdefault(r[1], []).append(r)
    done, report = [], []
    for rel, versions in by_path.items():
        first, last = versions[0], versions[-1]
        p = (root / rel).resolve()
        if root not in p.parents:  # only a tampered database could say this
            report.append((rel, "skipped: outside the workspace"))
            continue
        current = p.read_bytes() if p.is_file() else None
        if not force and (current is None or sha(current) != last[4]):
            report.append((rel, "skipped: changed after this run (undo the later change first, or --force)"))
            continue
        if first[2] == TOO_LARGE:
            report.append((rel, f"skipped: the earlier version was over {MAX_BYTES:,} bytes and was not kept"))
            continue
        if first[2] == SAVED:
            if not dry_run:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(first[3])
            report.append((rel, "restored"))
        else:
            if not dry_run and current is not None:
                p.unlink()
            report.append((rel, "removed (the run created it)"))
        done += [v[0] for v in versions]
    if done and not dry_run:
        stamp = iso(now or datetime.now())
        db.executemany("UPDATE file_versions SET undone_ts=? WHERE id=?", [(stamp, i) for i in done])
        db.commit()
    return report
