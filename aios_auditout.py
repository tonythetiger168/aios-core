"""Copies of the audit log outside aios.db: a webhook sink and chain-head anchors. Standard library only.

    python aios.py daemon ... --audit-sink https://siem.example.com/aios --anchor /mnt/backup/aios-anchors.jsonl
    python aios.py audit --anchor /mnt/backup/aios-anchors.jsonl           # one anchor now (cron)
    python aios.py audit --verify --anchors /mnt/backup/aios-anchors.jsonl

The hash chain catches an entry edited, removed or inserted by hand, but anyone who can write aios.db
can rebuild the whole chain. Both features put evidence where that person cannot reach:

* Sink: the daemon POSTs every new audit entry (written by any AIOS process on this database: chat,
  serve, a2a-serve, jobs) to a URL as JSON batches, from a background thread, so a slow collector never
  delays a job. Its position is kept in aios.db, so nothing is dropped: entries wait while the
  collector is down and go out when it is back (at least once; a batch may repeat after a failure,
  use each entry's `hash` to drop duplicates). Values of argument keys that look like secrets
  (password, token, api_key, ...) are replaced by "[redacted]" in the copy; results are sent as stored.
  HTTPS only, except to this machine. AIOS_AUDIT_SINK_TOKEN, when set, goes in `Authorization: Bearer`.
* Anchors: a JSON line {"ts", "id", "head"} appended to a file whenever the chain has grown. An anchor
  binds entry `id` to its hash: if the log is later rewritten up to that point, or cut back before it,
  `audit --verify --anchors FILE` and the daemon report it. A chain that does not verify is never
  anchored (that would vouch for the tampering). Keep the file on another disk or machine.
"""
import ipaddress
import json
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import aios_watchdog as wd

BATCH = 200
TIMEOUT = 10
MAX_BACKOFF = 300  # seconds between attempts while the collector keeps failing
FAILING_AFTER = 15 * 60  # seconds of failure before the watchdog raises audit-sink-failing
TOKEN_ENV = "AIOS_AUDIT_SINK_TOKEN"
SECRET_KEY = re.compile(r"pass(word|wd|phrase)?|secret|token|api[_-]?key|auth(orization)?|cookie|"
                        r"credential|private[_-]?key", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_sink(
    url TEXT PRIMARY KEY, last_id INTEGER DEFAULT 0, ok_ts TEXT, error TEXT, error_since TEXT);
"""


def check_url(url: str) -> str | None:
    """Why `url` is not an acceptable sink, or None."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("https", "http") or not u.hostname or u.username or u.password:
        return "give an http(s) URL with no user:password in it"
    if u.scheme == "http":
        host = u.hostname
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost"
        if not local:
            return "plain http only to this machine (127.0.0.1, ::1, localhost); use https"
    return None


def _redact(v):
    if isinstance(v, dict):
        return {k: "[redacted]" if SECRET_KEY.search(str(k)) else _redact(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_redact(x) for x in v]
    return v


def _args_copy(args: str):
    try:
        return _redact(json.loads(args))
    except (ValueError, TypeError):  # clipped at AUDIT_MAX: not JSON any more, keep it out
        return "[unparsed: clipped]"


def ensure(db: sqlite3.Connection):
    db.executescript(SCHEMA)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # a redirect could send the log elsewhere: count it as failure
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def ship_once(db: sqlite3.Connection, url: str, batch: int = BATCH, timeout: float = TIMEOUT,
              post=None, now=None) -> dict:
    """Send the next batch of unsent entries. Returns {'sent', 'left', 'error'}."""
    ensure(db)
    db.execute("INSERT OR IGNORE INTO audit_sink(url) VALUES(?)", (url,))
    last = db.execute("SELECT last_id FROM audit_sink WHERE url=?", (url,)).fetchone()[0] or 0
    rows = db.execute("SELECT id, ts, agent, tool, args, result, status, hash FROM audit WHERE id>? "
                      "ORDER BY id LIMIT ?", (last, batch)).fetchall()
    db.commit()
    if not rows:
        return {"sent": 0, "left": 0, "error": None}
    entries = [{"id": i, "ts": ts, "agent": ag, "tool": tl, "args": _args_copy(ar), "result": rs,
                "status": st, "hash": h} for i, ts, ag, tl, ar, rs, st, h in rows]
    body = json.dumps({"source": "aios", "host": socket.gethostname(), "entries": entries},
                      ensure_ascii=False).encode()
    stamp = wd.iso(now or datetime.now())
    error = None
    try:
        (post or _post)(url, body, timeout)
    except Exception as e:  # noqa: BLE001 - any failure leaves the batch in place for the next try
        error = f"{type(e).__name__}: {e}"[:300]
    if error:
        db.execute("UPDATE audit_sink SET error=?, error_since=COALESCE(error_since, ?) WHERE url=?",
                   (error, stamp, url))
    else:
        db.execute("UPDATE audit_sink SET last_id=?, ok_ts=?, error=NULL, error_since=NULL WHERE url=?",
                   (rows[-1][0], stamp, url))
    db.commit()
    left = db.execute("SELECT COUNT(*) FROM audit WHERE id>?",
                      (last if error else rows[-1][0],)).fetchone()[0]
    return {"sent": 0 if error else len(rows), "left": left, "error": error}


def _post(url: str, body: bytes, timeout: float):
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "aios-audit-sink"})
    if os.environ.get(TOKEN_ENV):
        req.add_header("Authorization", f"Bearer {os.environ[TOKEN_ENV]}")
    with _opener.open(req, timeout=timeout) as r:
        if not 200 <= r.status < 300:
            raise RuntimeError(f"HTTP {r.status}")
        r.read(1024)


def status(db: sqlite3.Connection) -> list[dict]:
    """Every sink this database has fed: how far behind it is and its last error."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='audit_sink'").fetchone():
        return []
    out = []
    for url, last, ok_ts, error, since in db.execute(
            "SELECT url, last_id, ok_ts, error, error_since FROM audit_sink ORDER BY url").fetchall():
        behind = db.execute("SELECT COUNT(*) FROM audit WHERE id>?", (last or 0,)).fetchone()[0]
        out.append({"url": url, "behind": behind, "ok_ts": ok_ts, "error": error, "error_since": since})
    return out


def failing(db: sqlite3.Connection, now=None) -> list[tuple[str, str]]:
    """(url, detail) for each sink failing for longer than FAILING_AFTER (a watchdog rule)."""
    now = now or datetime.now()
    out = []
    for s in status(db):
        if s["error"] and s["error_since"] and \
                (now - datetime.fromisoformat(s["error_since"])).total_seconds() >= FAILING_AFTER:
            out.append((s["url"], f"the audit sink {s['url']} has failed since {s['error_since']} "
                                  f"({s['behind']} entries waiting): {s['error']}"))
    return out


class Sink:
    """Background thread draining the audit log to `url`; its own connection, never blocks the caller."""

    def __init__(self, db_path: str, url: str, interval: float = 5, post=None):
        self.db_path, self.url, self.interval, self.post = db_path, url, interval, post
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="aios-audit-sink", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def drain(self, db: sqlite3.Connection, deadline: float | None = None) -> dict | None:
        r = None
        while deadline is None or time.monotonic() < deadline:
            r = ship_once(db, self.url, post=self.post)
            if r["error"] or not r["left"]:
                break
        return r

    def _run(self):
        db = sqlite3.connect(self.db_path)
        db.execute("PRAGMA busy_timeout=5000")
        wait = self.interval
        try:
            while not self._stop.is_set():
                try:
                    r = self.drain(db, time.monotonic() + 60)
                except sqlite3.Error:
                    r = {"error": "database busy"}
                wait = min(MAX_BACKOFF, wait * 2) if r and r["error"] else self.interval
                self._stop.wait(wait)
            try:  # last entries written before the stop (a bounded try, the collector may be down)
                self.drain(db, time.monotonic() + TIMEOUT)
            except sqlite3.Error:
                pass
        finally:
            db.close()

    def stop(self, timeout: float = TIMEOUT + 5):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout)


# ---- Anchors ----
def read_anchors(path: str) -> tuple[list[dict], str | None]:
    """The anchors in `path` and a problem with the file (None = fine). A missing file has none."""
    p = Path(path)
    if not p.exists():
        return [], None
    out = []
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            a = json.loads(line)
            if not (isinstance(a, dict) and isinstance(a.get("id"), int) and isinstance(a.get("head"), str)):
                raise ValueError
        except ValueError:
            return out, f"line {n} of {path} is not an anchor"
        out.append(a)
    return out, None


def verify_anchors(db: sqlite3.Connection, path: str) -> dict:
    """Check the chain, then that every anchored entry still has its anchored hash.
    Returns {'ok', 'anchors', 'problem', 'chain'} (chain: the verify_audit result)."""
    chain = wd.verify_audit(db)
    anchors, problem = read_anchors(path)
    if problem is None and not chain["ok"]:
        problem = chain["problem"]
    if problem is None:
        for a in anchors:
            row = db.execute("SELECT hash FROM audit WHERE id=?", (a["id"],)).fetchone()
            if row is None:
                problem = (f"entry {a['id']}, anchored at {a.get('ts')}, is gone: the log was cut back or "
                           "rebuilt since")
                break
            if row[0] != a["head"]:
                problem = (f"entry {a['id']} no longer has the hash anchored at {a.get('ts')}: the log up to "
                           "it was rewritten since")
                break
    return {"ok": problem is None, "anchors": len(anchors), "problem": problem, "chain": chain}


def anchor(db: sqlite3.Connection, path: str, now=None) -> dict:
    """Append the chain head to `path` if it moved since the last anchor, after checking the chain and
    the earlier anchors. Returns {'written', 'id', 'head', 'problem'}; nothing is written on a problem."""
    v = verify_anchors(db, path)
    if not v["ok"]:
        return {"written": False, "id": None, "head": None, "problem": v["problem"]}
    head = v["chain"]["head"]
    if head is None:
        return {"written": False, "id": None, "head": None, "problem": None}
    rid = db.execute("SELECT id FROM audit WHERE hash=? ORDER BY id DESC LIMIT 1", (head,)).fetchone()[0]
    anchors, _ = read_anchors(path)
    if anchors and anchors[-1]["id"] == rid and anchors[-1]["head"] == head:
        return {"written": False, "id": rid, "head": head, "problem": None}
    line = json.dumps({"ts": wd.iso(now or datetime.now()), "id": rid, "head": head}) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    return {"written": True, "id": rid, "head": head, "problem": None}
