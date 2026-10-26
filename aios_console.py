"""Local web console for aios-core: a view of the audit log, jobs, memory and skills, and (opt-in) the
place where a person answers approval requests from unattended runs.

    python aios.py console --home ~/aios-data             # read-only; prints a one-time URL to open
    python aios.py console --home ~/aios-data --approve   # also answer `daemon --approvals console`

Standard library only. Security model:
- Listens on 127.0.0.1 only. A random token (printed once) must be presented on the first visit; it is
  swapped for an HttpOnly, SameSite=Strict cookie, so other local users and other web pages cannot read it.
- The Host header must be 127.0.0.1/localhost on our port, which defeats DNS-rebinding attacks.
- Read-only by default: GET/HEAD only, and the database is opened with SQLite's read-only mode (never
  created). With --approve, exactly one write exists: POST /approvals sets a pending request to
  approved/denied. It needs the cookie, a per-start form token (CSRF), and a same-origin Origin header.
- Everything shown is HTML-escaped. The audit log holds attacker-controlled text (file contents, tool
  output), so the page carries no JavaScript at all and a CSP that forbids scripts.
"""
import hashlib
import hmac
import html
import http.server
import secrets
import sqlite3
import sys
import threading
import urllib.parse
from datetime import datetime
from pathlib import Path

COOKIE = "aios_console"
SHOW_MAX = 400  # chars of one audit argument/result cell
AUDIT_MAX = 500  # rows per audit page
ARGS_MAX = 20_000  # chars of arguments shown on an approval request (a person must see what they allow)
POST_MAX = 2048  # bytes of a form submission
CSP = ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'none'")
STYLE = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e5e5ea;--bad:#b3261e;--ok:#1e7d32;--warn:#8a5a00;--chip:#f2f2f7}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#f2f2f7;--muted:#a1a1a6;--line:#2c2c2e;--bad:#ff6b5f;
--ok:#5fd27a;--warn:#f0b54a;--chip:#232326}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
header{display:flex;gap:16px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
header b{margin-right:8px}a{color:inherit}nav a{text-decoration:none;padding:4px 8px;border-radius:6px}
nav a.on{background:var(--chip);font-weight:600}main{padding:16px;max-width:1200px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;vertical-align:top;padding:6px 8px;
border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:500}td pre{margin:0;white-space:pre-wrap;
word-break:break-word;font:12px/1.4 ui-monospace,monospace;max-height:12em;overflow:auto}
.ok{color:var(--ok)}.denied,.error,.timeout{color:var(--bad)}.skipped,.stopped{color:var(--warn)}
.muted{color:var(--muted)}.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.card{border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:140px}
.card div{font-size:22px;font-weight:600}.req{border:1px solid var(--line);border-radius:8px;padding:12px 14px;
margin-bottom:12px}.req pre{margin:8px 0;white-space:pre-wrap;word-break:break-word;font:12px/1.4 ui-monospace,monospace;
max-height:24em;overflow:auto;background:var(--chip);padding:8px;border-radius:6px}.warn{color:var(--warn);font-weight:600}
.approved{color:var(--ok)}.expired{color:var(--muted)}form{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
input,select,button{font:inherit;color:inherit;background:var(--chip);border:1px solid var(--line);
border-radius:6px;padding:4px 8px}.scroll{overflow-x:auto}td:first-child{white-space:nowrap}
"""
PAGES = (("/", "Overview"), ("/approvals", "Approvals"), ("/audit", "Audit"), ("/jobs", "Jobs"), ("/memory", "Memory"), ("/skills", "Skills"))


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def clip(v, n=SHOW_MAX) -> str:
    s = "" if v is None else str(v)
    return s if len(s) <= n else s[:n] + f"... (+{len(s) - n} chars)"


class Reader:
    """Read-only queries. Opens the database per request in SQLite read-only mode."""

    def __init__(self, db: Path):
        self.db = db

    def connect(self):
        if not self.db.exists():
            return None
        return sqlite3.connect(f"{self.db.resolve().as_uri()}?mode=ro", uri=True)

    def query(self, sql: str, params=()) -> list[tuple]:
        con = self.connect()
        if con is None:
            return []
        try:
            return con.execute(sql, params).fetchall()
        except sqlite3.OperationalError:  # table missing in an old or empty database
            return []
        finally:
            con.close()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def decide(db: Path, aid: int, approve: bool) -> bool:
    """The console's only write: answer a request that is still open. False if it was already
    answered, expired or never existed."""
    con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=rw", uri=True)  # never creates a database
    try:
        with con:
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name='approvals'").fetchone():
                return False  # a database from before approvals existed
            n = con.execute("UPDATE approvals SET status=?, decided_ts=? WHERE id=? AND status='pending' "
                            "AND expires>?", ("approved" if approve else "denied", now_iso(), aid, now_iso())).rowcount
        return n == 1
    finally:
        con.close()


def table(head: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="muted">{esc(empty)}</p>'
    th = "".join(f"<th>{esc(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def status(s) -> str:
    return f'<span class="{esc(s)}">{esc(s)}</span>'


def pre(v) -> str:
    return f"<pre>{esc(clip(v))}</pre>"


def options(values, selected, blank="all") -> str:
    out = [f'<option value="">{esc(blank)}</option>']
    out += [f'<option{" selected" if v == selected else ""}>{esc(v)}</option>' for v in values]
    return "".join(out)


def page_overview(r: Reader, q: dict) -> str:
    n = lambda sql: (r.query(sql) or [(0,)])[0][0]
    beat = r.query("SELECT ts FROM heartbeat WHERE name='daemon'")
    seen = f"seen {beat[0][0][11:16]}" if beat else "never ran"
    cards = [("Open alerts", n("SELECT COUNT(*) FROM alerts WHERE acked_ts IS NULL")),
             ("Daemon", seen),
             ("Tool calls", n("SELECT COUNT(*) FROM audit WHERE tool!='(run)'")),
             ("Refused", n("SELECT COUNT(*) FROM audit WHERE status='denied'")),
             ("Errors", n("SELECT COUNT(*) FROM audit WHERE status='error'")),
             ("Waiting for approval", (r.query("SELECT COUNT(*) FROM approvals WHERE status='pending' AND expires>?",
                                               (now_iso(),)) or [(0,)])[0][0]),
             ("Jobs", n("SELECT COUNT(*) FROM jobs")),
             ("Notes", n("SELECT COUNT(*) FROM facts")),
             ("Trusted skills", n("SELECT COUNT(*) FROM skills"))]
    html_cards = "".join(f'<div class="card"><span class="muted">{esc(k)}</span><div>{esc(v)}</div></div>'
                         for k, v in cards)
    rows = r.query("SELECT ts, agent, tool, args, result FROM audit WHERE status='denied' ORDER BY id DESC LIMIT 20")
    denied = table(["Time", "Agent", "Tool", "Arguments", "Reason"],
                   [[esc(ts), esc(a), esc(t), pre(ar), pre(res)] for ts, a, t, ar, res in rows],
                   "Nothing has been refused.")
    rows = r.query("SELECT ts, severity, rule, subject, detail FROM alerts WHERE acked_ts IS NULL "
                   "ORDER BY id DESC LIMIT 20")
    sev = {"critical": "denied", "warning": "skipped", "info": "muted"}
    alerts = table(["Time", "Severity", "Rule", "Subject", "Detail"],
                   [[esc(ts), f'<span class="{sev.get(sv, "")}">{esc(sv)}</span>', esc(ru), esc(su), esc(de)]
                    for ts, sv, ru, su, de in rows],
                   "No open alerts. The daemon checks the audit log every tick; acknowledge with `aios alerts --ack`.")
    return (f'<div class="cards">{html_cards}</div><h2>Open alerts</h2>{alerts}'
            f"<h2>Latest refusals</h2>{denied}")


def page_approvals(r: Reader, q: dict, csrf: str | None) -> str:
    """csrf None = this console was started without --approve: requests are shown, not answerable."""
    done = {"approved": "Approved.", "denied": "Denied.",
            "gone": "That request was already answered or has expired."}.get(q.get("r", ""), "")
    out = [f'<p class="ok">{esc(done)}</p>' if done else ""]
    if csrf is None:
        out.append('<p class="muted">Read-only: start the console with <code>--approve</code> to answer requests.</p>')
    pending = r.query("SELECT id, ts, agent, tool, args, tainted, expires FROM approvals "
                      "WHERE status='pending' AND expires>? ORDER BY id", (now_iso(),))
    if not pending:
        out.append('<p class="muted">Nothing is waiting. This page reloads every 5 seconds. Requests come from '
                   '<code>aios daemon --approvals console</code> or <code>aios serve --approvals console</code>.</p>')
    for aid, ts, agent, tool, args, tainted, expires in pending:
        warn = ('<p class="warn">This run has read untrusted content (a file or tool result). Check that YOU want '
                "this, not text inside it.</p>") if tainted else ""
        buttons = ""
        if csrf is not None:
            hidden = f'<input type="hidden" name="id" value="{esc(aid)}"><input type="hidden" name="csrf" value="{esc(csrf)}">'
            buttons = (f'<form method="post" action="/approvals">{hidden}'
                       '<button name="decision" value="approve">Approve</button>'
                       '<button name="decision" value="deny">Deny</button></form>')
        out.append(f'<div class="req"><b>#{esc(aid)}</b> <b>{esc(agent)}</b> wants to run <b>{esc(tool)}</b> '
                   f'<span class="muted">asked {esc(ts)} · closes {esc(expires)} (then refused)</span>{warn}'
                   '<div class="muted">Arguments, as the model wrote them:</div>'
                   f"<pre>{esc(clip(args, ARGS_MAX))}</pre>{buttons}</div>")
    rows = r.query("SELECT id, ts, agent, tool, status, decided_ts FROM approvals WHERE status!='pending' "
                   "OR expires<=? ORDER BY id DESC LIMIT 50", (now_iso(),))
    out.append("<h2>Answered</h2>" + table(
        ["#", "Asked", "Agent", "Tool", "Answer", "When"],
        [[esc(i), esc(ts), esc(a), esc(t), status("expired" if s == "pending" else s), esc(d or "")]
         for i, ts, a, t, s, d in rows], "No answered requests yet."))
    return "".join(out)


def page_audit(r: Reader, q: dict) -> str:
    agent, st, tool = q.get("agent", ""), q.get("status", ""), q.get("tool", "")
    try:
        limit = max(1, min(AUDIT_MAX, int(q.get("n", "100"))))
    except ValueError:
        limit = 100
    where, params = [], []
    for col, val in (("agent", agent), ("status", st), ("tool", tool)):
        if val:
            where.append(f"{col}=?")
            params.append(val)
    sql = "SELECT ts, agent, tool, status, args, result FROM audit"
    sql += (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    rows = r.query(sql, (*params, limit))
    agents = [a for (a,) in r.query("SELECT DISTINCT agent FROM audit ORDER BY agent")]
    tools = [t for (t,) in r.query("SELECT DISTINCT tool FROM audit ORDER BY tool")]
    form = (f'<form method="get" action="/audit"><select name="agent">{options(agents, agent, "all agents")}</select>'
            f'<select name="tool">{options(tools, tool, "all tools")}</select>'
            f'<select name="status">{options(["ok", "denied", "error", "finished", "budget", "max_steps", "loop"], st, "any status")}</select>'
            f'<input name="n" value="{esc(limit)}" size="4" aria-label="rows"><button>Filter</button></form>')
    body = table(["Time", "Agent", "Tool", "Status", "Arguments", "Result"],
                 [[esc(ts), esc(a), esc(t), status(s), pre(ar), pre(res)] for ts, a, t, s, ar, res in rows],
                 "No matching entries.")
    return form + '<p class="muted">Results are shown as recorded. Tool output may contain text an attacker wrote.</p>' + body


def page_jobs(r: Reader, q: dict) -> str:
    jobs = r.query("SELECT name, agent, schedule, next_run, last_run, last_status, enabled FROM jobs ORDER BY name")
    jt = table(["Job", "Agent", "Schedule", "Next run", "Last run", "Last status", "Enabled"],
               [[esc(n), esc(a), esc(s), esc(nx), esc(lr), status(ls or "-"), "yes" if en else "no"]
                for n, a, s, nx, lr, ls, en in jobs], "No jobs. Add one with `aios jobs add`.")
    runs = r.query("SELECT ts, job, status, output FROM job_runs ORDER BY id DESC LIMIT 100")
    rt = table(["Time", "Job", "Status", "Output"],
               [[esc(ts), esc(j), status(s), pre(o)] for ts, j, s, o in runs], "No runs yet.")
    return f"<h2>Jobs</h2>{jt}<h2>Recent runs</h2>{rt}"


def page_memory(r: Reader, q: dict) -> str:
    agents = [a for (a,) in r.query("SELECT DISTINCT agent FROM facts ORDER BY agent")]
    agent = q.get("agent", "") or (agents[0] if agents else "")
    form = (f'<form method="get" action="/memory"><select name="agent">{options(agents, agent, "choose agent")}</select>'
            "<button>Show</button></form>")
    rows = r.query("SELECT key, value, source, ts FROM facts WHERE agent=? ORDER BY key", (agent,))
    body = table(["Key", "Value", "Written by", "When"],
                 [[esc(k), pre(v), f'<span class="{"skipped" if s == "tainted" else ""}">{esc(s or "unknown")}</span>',
                   esc(ts)] for k, v, s, ts in rows],
                 "No notes for this agent.")
    note = ('<p class="muted">"tainted" notes were saved by a run that had read untrusted content; '
            "the model sees them as unverified.</p>")
    return form + note + body


def page_skills(r: Reader, q: dict) -> str:
    rows = r.query("SELECT name, version, sha256, ts, forced FROM skills ORDER BY name")
    return table(["Skill", "Version", "Pinned digest", "Trusted at", "Forced"],
                 [[esc(n), esc(v), f"<pre>{esc(h)}</pre>", esc(ts), "yes" if f else "no"]
                  for n, v, h, ts, f in rows], "No trusted skills. Review one with `aios skills scan`.")


ROUTES = {"/": page_overview, "/audit": page_audit, "/jobs": page_jobs, "/memory": page_memory,
          "/skills": page_skills}


def render(path: str, body: str, home: Path, mode: str = "read-only", refresh: bool = False) -> bytes:
    nav = "".join(f'<a href="{p}"{" class=on" if p == path else ""}>{esc(t)}</a>' for p, t in PAGES)
    title = dict(PAGES).get(path, "")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>AIOS Console</title>'
            + ('<meta http-equiv="refresh" content="5">' if refresh else "") +
            f"<style>{STYLE}</style></head><body><header><b>AIOS Console</b><nav>{nav}</nav>"
            f'<span class="muted">{esc(mode)} · {esc(home)}</span></header><main><h1>{esc(title)}</h1>{body}'
            "</main></body></html>").encode()


def make_handler(home: Path, token: str, port_ref: dict, approve: bool = False):
    reader = Reader(home / "aios.db")
    # Form token: proves a POST came from a page this console served (another site cannot read it).
    csrf = hmac.new(token.encode(), b"aios-console-csrf", hashlib.sha256).hexdigest() if approve else None
    routes = dict(ROUTES, **{"/approvals": lambda r, q: page_approvals(r, q, csrf)})
    mode = "approvals on" if approve else "read-only"

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "aios-console"
        sys_version = ""

        def log_message(self, *a):  # keep the terminal quiet; requests are not interesting to log
            pass

        def send(self, code: int, body: bytes = b"", ctype="text/html; charset=utf-8", extra=()):
            self.send_response(code)
            for k, v in (("Content-Type", ctype), ("Content-Length", str(len(body))),
                         ("Content-Security-Policy", CSP), ("X-Content-Type-Options", "nosniff"),
                         ("X-Frame-Options", "DENY"), ("Referrer-Policy", "same-origin"),  # no-referrer would make browsers send "Origin: null" on our own forms
                         ("Cache-Control", "no-store"), *extra):
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def host_ok(self) -> bool:
            port = port_ref["port"]
            return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def authed(self) -> bool:
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE and hmac.compare_digest(v, token):
                    return True
            return False

        def do_GET(self):
            if not self.host_ok():  # DNS rebinding: a foreign name pointing at 127.0.0.1
                return self.send(403, b"forbidden host", "text/plain")
            url = urllib.parse.urlsplit(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            if "t" in q:  # first visit: trade the token for a cookie, then drop it from the address bar
                if not hmac.compare_digest(q["t"], token):
                    return self.send(403, b"wrong token", "text/plain")
                rest = urllib.parse.urlencode({k: v for k, v in q.items() if k != "t"})
                return self.send(303, extra=(("Location", url.path + ("?" + rest if rest else "")),
                                             ("Set-Cookie", f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/")))
            if not self.authed():
                return self.send(403, b"open the URL that `aios console` printed (it carries the access token)",
                                 "text/plain")
            fn = routes.get(url.path)
            if fn is None:
                return self.send(404, b"not found", "text/plain")
            if not reader.db.exists():
                body = f'<p class="muted">No database yet at {esc(reader.db)}. Run aios once with this --home.</p>'
            else:
                body = fn(reader, q)
            refresh = url.path == "/approvals" and "Nothing is waiting." in body
            self.send(200, render(url.path, body, home, mode, refresh))

        do_HEAD = do_GET

        def refuse_write(self):
            self.send(405, b"read-only", "text/plain", extra=(("Allow", "GET, HEAD"),))

        def do_POST(self):
            if not approve or urllib.parse.urlsplit(self.path).path != "/approvals":
                return self.refuse_write()
            if not self.host_ok():
                return self.send(403, b"forbidden host", "text/plain")
            if not self.authed():
                return self.send(403, b"not signed in", "text/plain")
            port = port_ref["port"]
            if self.headers.get("Origin") not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                return self.send(403, b"cross-origin request refused", "text/plain")
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self.send(411, b"length required", "text/plain")
            if not 0 < n <= POST_MAX:
                return self.send(413, b"too large", "text/plain")
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}
            if not hmac.compare_digest(form.get("csrf", ""), csrf):
                return self.send(403, b"stale form: reload the page", "text/plain")
            decision, aid = form.get("decision"), form.get("id", "")
            if decision not in ("approve", "deny") or not aid.isdigit():
                return self.send(400, b"bad request", "text/plain")
            ok = decide(reader.db, int(aid), decision == "approve")
            result = ("approved" if decision == "approve" else "denied") if ok else "gone"
            self.send(303, extra=(("Location", f"/approvals?r={result}"),))

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse_write

    return Handler


def start(home: str = ".", port: int = 8765, token: str | None = None, approve: bool = False):
    """Start the console in a background thread. Returns (server, url_with_token)."""
    base = Path(home).resolve()
    token = token or secrets.token_urlsafe(24)
    port_ref = {"port": port}
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(base, token, port_ref, approve))
    port_ref["port"] = srv.server_address[1]  # port 0 = any free port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port_ref['port']}/?t={token}"


def run(home: str = ".", port: int = 8765, open_browser: bool = False, approve: bool = False) -> int:
    if approve and not (Path(home) / "aios.db").exists():
        print(f"no aios.db in {Path(home).resolve()}: start the daemon (or serve) with this --home first",
              file=sys.stderr)
        return 1
    try:
        srv, url = start(home, port, approve=approve)
    except OSError as e:
        print(f"cannot listen on 127.0.0.1:{port}: {e}", file=sys.stderr)
        return 1
    print(f"[aios] console ({'approvals on' if approve else 'read-only'}) for {Path(home).resolve()}\n  open: {url}\n"
          "  The link carries an access token: do not share it. Ctrl+C to stop.", file=sys.stderr)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
    return 0
