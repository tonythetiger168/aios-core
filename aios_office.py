"""3D office for the local web console: every agent is a person in an office, and where it stands
shows what it is doing right now, read from the audit log, approvals, holds and jobs.

    python aios.py console --home ~/aios-data      # then open the "Office" page

Places (the same rules every time, so the picture can be trusted):
- Lobby: the entrance and reception. Agents whose risky call waits for a person queue at the
  approval counter; agents whose scheduled job is due in the next few minutes sit in the waiting area.
- Open office: an agent that is working (it made a tool call in the last ACTIVE_S seconds) sits at its desk.
- Library: working with memory or the web (remember, recall, search_memory, fetch_url, ...).
- Server room: running code or an MCP server's tool.
- Talk lounge: two agents talking to each other (agent__<name> / a2a__<name>), with what they said.
- Meeting room: a discussion of three or more agents (a swarm, or a chain of hand-offs).
- Security office: held by the supervisor, or just refused by a rule.
- Break room: everyone else (idle).

Security: the rest of the console carries no JavaScript. This page does, under its own CSP
(script-src 'self'): the script is fixed code from this file, its only input is /office.json, and it
draws names and messages with canvas fillText and textContent, never as HTML. Messages are clipped
and come from the audit log, which the Audit page already shows.
"""
import json
import re
from datetime import datetime, timedelta

ACTIVE_S = 90  # a tool call this recent means the agent is busy with it
TALK_S = 120  # a conversation between agents stays on screen this long after it ends
DUE_S = 300  # a job due this soon waits in the lobby
MAX_AGENTS = 80  # people drawn at once (the busiest first)
SCAN_ROWS = 20_000  # newest audit rows read per refresh
SAY_MAX = 140  # chars of a message shown in a speech bubble
RUN_ENTRY, CARRY_ENTRY = "(run)", "(provenance)"
LIBRARY_TOOLS = {"remember", "recall", "list_facts", "search_memory", "fetch_url"}
SERVER_TOOLS = {"run_python"}
PLACES = ("lobby", "approval", "waiting", "desk", "library", "server", "talk", "meeting", "security", "break")
PLACE_NAMES = {"lobby": "Lobby", "approval": "Approval counter", "waiting": "Waiting area", "desk": "Open office",
               "library": "Library", "server": "Server room", "talk": "Talk lounge", "meeting": "Meeting room",
               "security": "Security office", "break": "Break room"}
CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")


def _age(ts, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(str(ts))).total_seconds()
    except ValueError:
        return float("inf")


def _clip(s, n=SAY_MAX) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _args(raw) -> dict:
    """Audit arguments are JSON clipped to AUDIT_MAX: fall back to fishing out the fields we need."""
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        out = {}
        for key in ("task", "helper"):
            m = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)', raw or "")
            if m:
                out[key] = m.group(1)
        return out


def _callee(tool: str) -> str:
    for p in ("agent__", "a2a__"):
        if tool.startswith(p):
            return tool[len(p):]
    return ""


def place_for_tool(tool: str) -> str:
    if tool in LIBRARY_TOOLS:
        return "library"
    if tool in SERVER_TOOLS or ("__" in tool and not _callee(tool)):
        return "server"  # run_python, or an MCP server's tool (server__tool)
    return "desk"


def state(query, now: datetime | None = None) -> dict:
    """query(sql, params) -> rows (read-only). Returns what /office.json serves."""
    now = now or datetime.now()
    iso = now.isoformat(timespec="seconds")
    last = query("SELECT a.agent, a.tool, a.status, a.ts FROM audit a JOIN (SELECT agent, MAX(id) m FROM audit "
                 "WHERE id > (SELECT COALESCE(MAX(id), 0) FROM audit) - ? AND tool != ? GROUP BY agent) b "
                 "ON a.id = b.m", (SCAN_ROWS, CARRY_ENTRY))
    since = (now - timedelta(seconds=TALK_S)).isoformat(timespec="seconds")
    calls = query("SELECT agent, tool, args, result, ts FROM audit WHERE id > (SELECT COALESCE(MAX(id), 0) FROM audit)"
                  " - ? AND ts >= ? AND (tool LIKE 'agent\\_\\_%' ESCAPE '\\' OR tool LIKE 'a2a\\_\\_%' ESCAPE '\\' "
                  "OR tool = 'swarm') ORDER BY id", (SCAN_ROWS, since))
    pending = query("SELECT agent, tool FROM approvals WHERE status='pending' AND expires>? ORDER BY id", (iso,))
    holds = dict(query("SELECT agent, reason FROM holds"))
    jobs = query("SELECT name, next_run, enabled FROM jobs")

    people: dict[str, dict] = {}

    def person(name, kind="agent") -> dict:
        name = _clip(name, 48)
        return people.setdefault(name, {"name": name, "kind": kind, "place": "break", "state": "idle",
                                        "tool": "", "with": [], "says": "", "busy": 0})

    for name, nr, enabled in jobs:
        p = person(f"job-{name}")
        if enabled and nr and -DUE_S < -_age(nr, now) <= DUE_S:
            p.update(place="waiting", state=f"job due at {str(nr)[11:16]}")
    for name, tool, status, ts in last:
        p, age = person(name), _age(ts, now)
        if tool == RUN_ENTRY or age > ACTIVE_S:
            continue
        p["busy"] = 1
        if status == "denied":
            p.update(place="security", state=f"refused: {tool}", tool=tool)
        else:
            p.update(place=place_for_tool(tool), state=f"using {tool}", tool=tool)

    # Conversations: who talked to whom in the last TALK_S seconds, and what they said.
    edges = []
    for name, tool, raw, result, ts in calls:
        a = _args(raw)
        caller = person(name)
        if tool == "swarm":
            helpers = {t.get("helper") for t in a.get("tasks") or () if isinstance(t, dict)}
            helpers |= {a.get("helper"), (a.get("reduce") or {}).get("helper") if isinstance(a.get("reduce"), dict) else None}
            helpers = sorted(h for h in helpers if isinstance(h, str) and h)
            caller["says"] = _clip(f"Team, let's split this up ({len(helpers)} helpers).")
            for h in helpers:
                edges.append((caller["name"], person(_callee(h) or h, "guest" if h.startswith("a2a__") else "agent")["name"]))
            continue
        other = _callee(tool)
        remote = tool.startswith("a2a__")
        callee = person(f"{other} (remote)" if remote else other, "guest" if remote else "agent")
        caller["says"] = _clip(f"@{callee['name']} {a.get('task', '')}")
        callee["says"] = _clip(result)
        edges.append((caller["name"], callee["name"]))

    groups: dict[str, set] = {}  # union of everyone in a conversation
    for x, y in edges:
        g = groups.get(x, {x}) | groups.get(y, {y})
        for n in g:
            groups[n] = g
    for name, g in groups.items():
        p, others = people[name], sorted(g - {name})
        place = "talk" if len(g) == 2 else "meeting"
        more = f" +{len(others) - 4}" if len(others) > 4 else ""
        p.update(place=place, busy=1, with_=others[:12], group=min(g),
                 state=("talking with " if place == "talk" else "in a discussion with ") + ", ".join(others[:4]) + more)
        p["with"] = p.pop("with_")

    for name, tool in pending:
        p = person(name)
        p.update(place="approval", state=f"waiting for your approval: {tool}", tool=tool, busy=1)
    for name, reason in holds.items():
        p = person(name)
        p.update(place="security", state=f"held by the supervisor: {_clip(reason, 80)}", busy=1)

    agents = sorted(people.values(), key=lambda p: (-p["busy"], p["name"]))[:MAX_AGENTS]
    for p in agents:
        p.pop("busy")
    counts = {k: 0 for k in PLACES}
    for p in agents:
        counts[p["place"]] += 1
    return {"ts": iso, "agents": agents, "counts": counts, "waiting_for_you": bool(pending),
            "places": PLACE_NAMES}


JS = r"""
'use strict';
(function () {
  // Fixed scene. x to the right, z towards the entrance, y up. One unit is about a metre.
  var ROOMS = {
    meeting: {x0: 0, z0: 0, x1: 10, z1: 8, floor: '#c7cfe0', label: 'Meeting room', door: [5, 9]},
    server: {x0: 10, z0: 0, x1: 18, z1: 8, floor: '#b9c3c9', label: 'Server room', door: [14, 9]},
    library: {x0: 18, z0: 0, x1: 26, z1: 8, floor: '#d8ccb4', label: 'Library', door: [22, 9]},
    security: {x0: 26, z0: 0, x1: 32, z1: 8, floor: '#c9c1d6', label: 'Security office', door: [29, 9]},
    desk: {x0: 0, z0: 8, x1: 24, z1: 18, floor: '#e6e3dc', label: 'Open office', door: null},
    brk: {x0: 24, z0: 10, x1: 32, z1: 18, floor: '#ead5bf', label: 'Break room', door: [28, 9]},
    talk: {x0: 0, z0: 18, x1: 9, z1: 28, floor: '#c8dbc4', label: 'Talk lounge', door: [4, 17]},
    lobby: {x0: 9, z0: 18, x1: 32, z1: 28, floor: '#ddd2c2', label: 'Lobby', door: [20, 17]}
  };
  var AREA = {approval: 'lobby', waiting: 'lobby', lobby: 'lobby', 'break': 'brk'};
  function room(place) { return ROOMS[AREA[place] || place] || ROOMS.brk; }
  var SLOTS = {desk: [], meeting: [], server: [], library: [], security: [], 'break': [], talk: [],
               approval: [], waiting: [], lobby: []};
  var boxes = [];  // [x, y, z, w, h, d, color]
  function box(x, y, z, w, h, d, c) { boxes.push([x, y, z, w, h, d, c]); }
  var WALL = '#f4f1ea', WALL_H = 1.1;
  function wallX(z, a, b) { box(a, 0, z - 0.08, b - a, WALL_H, 0.16, WALL); }
  function wallZ(x, a, b) { box(x - 0.08, 0, a, 0.16, WALL_H, b - a, WALL); }
  // Outer walls (front has the glass entrance) and inner walls with doorways.
  wallX(0, 0, 32); wallZ(0, 0, 28); wallZ(32, 0, 28); wallX(28, 0, 18); wallX(28, 22, 32);
  box(18, 0, 27.9, 0.2, 2.2, 0.2, '#8a8f99'); box(21.8, 0, 27.9, 0.2, 2.2, 0.2, '#8a8f99');
  box(18, 2.0, 27.9, 4, 0.2, 0.2, '#8a8f99');
  wallX(8, 0, 4); wallX(8, 6, 13); wallX(8, 15, 21); wallX(8, 23, 28); wallX(8, 30, 32);
  wallZ(10, 0, 8); wallZ(18, 0, 8); wallZ(26, 0, 8);
  wallX(10, 24, 27); wallX(10, 29, 32); wallZ(24, 10, 18);
  wallX(18, 0, 3); wallX(18, 5, 9); wallX(18, 9, 14); wallX(18, 26, 32); wallZ(9, 18, 22); wallZ(9, 24, 28);
  // Open office: desks with monitors, the agent sits on the chair side.
  [2.5, 6, 9.5, 13, 16.5, 20].forEach(function (x) {
    [[11.5, 1], [15.5, -1]].forEach(function (row) {
      var z = row[0], s = row[1];
      box(x - 1.2, 0, z - 0.5, 2.4, 0.75, 1, '#b58b5a');
      box(x - 0.45, 0.75, z - 0.3 * s - 0.05, 0.9, 0.55, 0.08, '#2b2f36');
      box(x - 0.3, 0, z + 0.9 * s - 0.25, 0.6, 0.45, 0.5, '#4a5568');
      SLOTS.desk.push([x, z + 0.95 * s]);
    });
  });
  box(22.6, 0, 11, 0.8, 1.2, 0.8, '#3f7d4f'); box(0.4, 0, 9, 0.8, 1.2, 0.8, '#3f7d4f');
  // Meeting room: a long table for discussions.
  box(2.5, 0, 3, 5, 0.75, 2, '#7a5634');
  box(4.2, 0.75, 0.1, 1.6, 1.0, 0.08, '#e8edf3');  // whiteboard
  [[3, 2.3], [5, 2.3], [7, 2.3], [3, 5.7], [5, 5.7], [7, 5.7], [1.7, 4], [8.3, 4], [2, 7], [8, 7], [5, 7.2], [1.2, 1.2]]
    .forEach(function (p) { SLOTS.meeting.push(p); });
  // Server room: racks with lights, agents stand in the aisle.
  for (var i = 0; i < 4; i++) { box(10.7 + i * 1.8, 0, 0.5, 1.2, 2.0, 1.0, '#23262c'); box(10.7 + i * 1.8, 0, 5.5, 1.2, 2.0, 1.0, '#23262c'); }
  [[11.3, 3.2], [13.1, 3.2], [14.9, 3.2], [16.7, 3.2], [12.2, 4.6], [15.8, 4.6]].forEach(function (p) { SLOTS.server.push(p); });
  // Library: shelves and a reading table.
  [18.6, 20.6, 22.6, 24.6].forEach(function (x) { box(x, 0, 0.3, 1.6, 2.2, 0.6, '#6b4a2b'); });
  box(20.5, 0, 4, 3, 0.75, 1.4, '#9c7a4f');
  [[21, 3.3], [23, 3.3], [21, 6.1], [23, 6.1], [19.6, 4.7], [24.4, 4.7]].forEach(function (p) { SLOTS.library.push(p); });
  // Security office: a desk with screens.
  box(27.5, 0, 2, 3, 0.8, 1, '#4b4f5c'); box(27.8, 0.8, 2.1, 2.4, 0.6, 0.1, '#1b1e24');
  [[28, 4], [30, 4], [29, 5.6], [27.6, 6.8], [30.4, 6.8]].forEach(function (p) { SLOTS.security.push(p); });
  // Break room: coffee counter, round table, sofa.
  box(31, 0, 10.6, 0.8, 1.0, 4, '#a0522d'); box(31.1, 1.0, 11, 0.5, 0.4, 0.5, '#d9d9d9');
  box(26.5, 0, 13, 1.6, 0.75, 1.6, '#c89b6d'); box(25, 0, 16.4, 4, 0.5, 1.2, '#7b9e87');
  [[26, 12.5], [28.6, 12.5], [26, 15.2], [28.6, 15.2], [25.6, 16.2], [27, 16.2], [28.4, 16.2], [30.2, 11.4], [30.2, 13.2]]
    .forEach(function (p) { SLOTS['break'].push(p); });
  // Talk lounge: armchairs in pairs, a coffee table between each pair.
  [[2.5, 20.5], [2.5, 25]].forEach(function (c) {
    box(c[0] + 0.7, 0, c[1] - 0.5, 1.2, 0.45, 1, '#e2c58f');
    box(c[0] - 0.6, 0, c[1] - 0.6, 0.9, 0.6, 1.2, '#5f7f9b'); box(c[0] + 2.5, 0, c[1] - 0.6, 0.9, 0.6, 1.2, '#5f7f9b');
    SLOTS.talk.push([c[0] - 0.15, c[1]], [c[0] + 2.95, c[1]]);
  });
  box(7.6, 0, 19, 0.8, 1.3, 0.8, '#3f7d4f');
  // Lobby: reception desk, approval counter, waiting sofas, plants, a rug at the entrance.
  box(14, 0, 21, 4, 1.05, 1, '#3d5a80'); box(14, 0, 22, 1, 1.05, 1.2, '#3d5a80');
  box(23.5, 0, 20.5, 3.5, 1.05, 0.9, '#98c1d9');
  for (var q = 0; q < 6; q++) SLOTS.approval.push([25.2, 22 + q * 0.9]);
  box(28.5, 0, 24.2, 3, 0.5, 1, '#c96f53'); box(28.5, 0, 26.4, 3, 0.5, 1, '#c96f53');
  [[29, 25.4], [30.2, 25.4], [31.3, 25.4], [29, 23.8], [30.2, 23.8], [31.3, 23.8]].forEach(function (p) { SLOTS.waiting.push(p); });
  box(10, 0, 26.6, 0.8, 1.3, 0.8, '#3f7d4f'); box(31, 0, 19, 0.8, 1.3, 0.8, '#3f7d4f');
  [[12, 25], [13.5, 25], [15, 25]].forEach(function (p) { SLOTS.lobby.push(p); });
  var RECEPTION = [16, 20.3];

  var canvas = document.getElementById('office'), ctx = canvas.getContext('2d');
  var list = document.getElementById('office-list'), info = document.getElementById('office-info');
  var stamp = document.getElementById('office-ts');
  var cam = {yaw: 2.55, pitch: 0.9, dist: 54, tx: 16, tz: 15};
  var people = {}, selected = null, waitingForYou = false, light = norm([0.4, 1, 0.3]);
  var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;

  function norm(v) { var l = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; }
  function hue(s) { var h = 0; for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0; return h % 360; }
  function shade(hex, k) {
    var n = parseInt(hex.slice(1), 16), r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    return 'rgb(' + Math.round(r * k) + ',' + Math.round(g * k) + ',' + Math.round(b * k) + ')';
  }
  function view(x, y, z) {  // world -> camera space
    x -= cam.tx; z -= cam.tz;
    var cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw), cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch);
    var x1 = x * cy - z * sy, z1 = x * sy + z * cy;
    var y2 = y * cp + z1 * sp, z2 = -y * sp + z1 * cp;
    return [x1, y2, z2 + cam.dist];
  }
  function rot(n) { var v = view(n[0] + cam.tx, n[1], n[2] + cam.tz), o = view(cam.tx, 0, cam.tz); return [v[0] - o[0], v[1] - o[1], v[2] - o[2]]; }
  var W = 0, H = 0, F = 0;
  function proj(c) { return [W / 2 - F * c[0] / c[2], H / 2 - F * c[1] / c[2]]; }
  function resize() {
    var r = canvas.getBoundingClientRect(), d = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, r.width * d); canvas.height = Math.max(1, r.height * d);
    ctx.setTransform(d, 0, 0, d, 0, 0); W = r.width; H = r.height; F = Math.min(W, H) * 1.6;
  }
  function quad(pts, n, color, items) {
    var cs = pts.map(function (p) { return view(p[0], p[1], p[2]); });
    if (cs.some(function (c) { return c[2] < 0.5; })) return;
    var rn = rot(n), cx = 0, cy = 0, cz = 0;
    cs.forEach(function (c) { cx += c[0] / 4; cy += c[1] / 4; cz += c[2] / 4; });
    if (rn[0] * cx + rn[1] * cy + rn[2] * cz >= 0) return;  // facing away
    var k = 0.6 + 0.4 * Math.max(0, n[0] * light[0] + n[1] * light[1] + n[2] * light[2]);
    items.push({z: cz, draw: function () {
      ctx.beginPath(); cs.forEach(function (c, i) { var p = proj(c); i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]); });
      ctx.closePath(); ctx.fillStyle = shade(color, k); ctx.fill();
      ctx.strokeStyle = 'rgba(0,0,0,0.12)'; ctx.lineWidth = 0.6; ctx.stroke();
    }});
  }
  function addBox(b, items) {
    var x = b[0], y = b[1], z = b[2], X = x + b[3], Y = y + b[4], Z = z + b[5], c = b[6];
    quad([[x, Y, z], [X, Y, z], [X, Y, Z], [x, Y, Z]], [0, 1, 0], c, items);
    quad([[x, y, Z], [X, y, Z], [X, Y, Z], [x, Y, Z]], [0, 0, 1], c, items);
    quad([[x, y, z], [X, y, z], [X, Y, z], [x, Y, z]], [0, 0, -1], c, items);
    quad([[X, y, z], [X, y, Z], [X, Y, Z], [X, Y, z]], [1, 0, 0], c, items);
    quad([[x, y, z], [x, y, Z], [x, Y, Z], [x, Y, z]], [-1, 0, 0], c, items);
  }

  // Where each person goes: a free seat in its place, kept while it stays there.
  function assign(list) {
    var used = {};
    Object.keys(SLOTS).forEach(function (k) { used[k] = {}; });
    var order = list.slice().sort(function (a, b) {
      return (a.group || a.name) < (b.group || b.name) ? -1 : (a.group || a.name) > (b.group || b.name) ? 1 : a.name < b.name ? -1 : 1;
    });
    // Keep seats of people who did not move, then seat the rest.
    order.forEach(function (a) { var p = people[a.name]; if (p && p.place === a.place && p.slot != null && !used[a.place][p.slot]) used[a.place][p.slot] = a.name; });
    order.forEach(function (a) {
      var p = people[a.name];
      if (p && p.place === a.place && used[a.place][p.slot] === a.name) { a.slot = p.slot; return; }
      var s = SLOTS[a.place] || SLOTS['break'], i = 0;
      while (used[a.place][i]) i++;
      used[a.place][i] = a.name; a.slot = i;
    });
  }
  function spot(place, i) {
    var s = SLOTS[place] || SLOTS['break'];
    if (i < s.length) return s[i];
    var r = room(place), w = r.x1 - r.x0 - 2, n = i - s.length, cols = Math.max(1, Math.floor(w / 1.1));
    return [r.x0 + 1 + (n % cols) * 1.1, r.z1 - 1 - Math.floor(n / cols) * 1.1];
  }
  function route(p, place, target) {
    var a = room(p.place), b = room(place), path = [];
    if (a !== b) { if (a.door) path.push(a.door); if (b.door) path.push(b.door); }
    path.push(target);
    return path;
  }
  function update(data) {
    var now = Date.now(), seen = {};
    waitingForYou = !!data.waiting_for_you;
    assign(data.agents);
    data.agents.forEach(function (a) {
      seen[a.name] = 1;
      var target = spot(a.place, a.slot), p = people[a.name];
      if (!p) {  // new arrivals come in through the front door
        p = people[a.name] = {x: 20, z: 29.5, path: [[20, 26]], place: 'lobby', born: now};
      }
      if (p.place !== a.place || p.slot !== a.slot) p.path = (p.path || []).concat(route(p, a.place, target));
      p.place = a.place; p.slot = a.slot; p.data = a;
      p.color = a.kind === 'guest' ? '#6b7280' : 'hsl(' + hue(a.name) + ',62%,52%)';
    });
    Object.keys(people).forEach(function (n) { if (!seen[n]) delete people[n]; });
    if (selected && !people[selected]) selected = null;
    stamp.textContent = 'updated ' + data.ts.slice(11);
    renderList(data);
  }
  function renderList(data) {
    while (list.firstChild) list.removeChild(list.firstChild);
    var names = data.places || {};
    var byPlace = {};
    data.agents.forEach(function (a) { (byPlace[a.place] = byPlace[a.place] || []).push(a); });
    Object.keys(names).forEach(function (k) {
      if (!byPlace[k]) return;
      var h = document.createElement('h3'); h.textContent = names[k] + ' (' + byPlace[k].length + ')'; list.appendChild(h);
      byPlace[k].forEach(function (a) {
        var li = document.createElement('button');
        li.type = 'button'; li.className = 'who' + (selected === a.name ? ' on' : '');
        var b = document.createElement('b'); b.textContent = a.name;
        var s = document.createElement('span'); s.textContent = ' ' + a.state;
        li.appendChild(b); li.appendChild(s);
        li.addEventListener('click', function () { selected = a.name; showInfo(); renderList(data); });
        list.appendChild(li);
      });
    });
    if (!data.agents.length) { var p = document.createElement('p'); p.textContent = 'No agents yet: run one and it will walk in.'; list.appendChild(p); }
    showInfo();
  }
  function showInfo() {
    var p = selected && people[selected];
    info.textContent = p ? p.data.name + ': ' + p.data.state + (p.data.says ? '\n“' + p.data.says + '”' : '') : 'Drag to turn the office, scroll to zoom, click a person.';
  }

  function step(dt) {
    Object.keys(people).forEach(function (n) {
      var p = people[n], t = p.path && p.path[0];
      p.walking = false;
      if (!t) return;
      var dx = t[0] - p.x, dz = t[1] - p.z, d = Math.hypot(dx, dz), v = 4.5 * dt;
      if (d <= v) { p.x = t[0]; p.z = t[1]; p.path.shift(); }
      else { p.x += dx / d * v; p.z += dz / d * v; p.walking = true; p.face = Math.atan2(dx, dz); }
    });
  }
  function figure(x, z, color, label, kind, items, extra) {
    var foot = view(x, 0, z);
    if (foot[2] < 0.5) return;
    items.push({z: foot[2] - 0.3, draw: function () {
      var t = performance.now() / 1000, bob = extra && extra.walking ? Math.abs(Math.sin(t * 9)) * 0.08 : 0;
      var f = proj(foot), top = proj(view(x, 1.15 + bob, z)), head = proj(view(x, 1.45 + bob, z));
      var s = F / foot[2], bw = 0.5 * s, hr = 0.26 * s;
      ctx.fillStyle = 'rgba(0,0,0,0.18)'; ctx.beginPath(); ctx.ellipse(f[0], f[1], bw * 0.8, bw * 0.3, 0, 0, 7); ctx.fill();
      ctx.fillStyle = color; ctx.beginPath();
      ctx.moveTo(f[0] - bw / 2, f[1]); ctx.lineTo(top[0] - bw / 2, top[1] + hr * 0.3); ctx.quadraticCurveTo(top[0], top[1] - hr * 0.4, top[0] + bw / 2, top[1] + hr * 0.3);
      ctx.lineTo(f[0] + bw / 2, f[1]); ctx.closePath(); ctx.fill();
      ctx.fillStyle = kind === 'human' ? '#e0b48a' : kind === 'guest' ? '#c7ccd4' : '#f1d7b8';
      ctx.beginPath(); ctx.arc(head[0], head[1], hr, 0, 7); ctx.fill();
      if (kind === 'agent' || kind === 'guest') {  // a small antenna marks an agent
        ctx.strokeStyle = color; ctx.lineWidth = Math.max(1, s * 0.04); ctx.beginPath();
        ctx.moveTo(head[0], head[1] - hr); ctx.lineTo(head[0], head[1] - hr * 1.8); ctx.stroke();
        ctx.beginPath(); ctx.arc(head[0], head[1] - hr * 1.9, Math.max(1.5, s * 0.05), 0, 7); ctx.fillStyle = color; ctx.fill();
      }
      if (extra) extra.screen = [head[0], head[1], hr];
      ctx.font = '600 ' + Math.max(9, Math.min(14, s * 0.32)) + 'px system-ui,sans-serif';
      ctx.textAlign = 'center'; ctx.fillStyle = dark ? '#f2f2f7' : '#1d1d1f';
      ctx.fillText(label, head[0], head[1] - hr * 2.4);
    }});
  }
  function bubble(p) {
    var d = p.data;
    var text = d.says || (d.place === 'approval' ? '… waiting for you' : d.place === 'security' ? '!' : '');
    if (!text || !p.screen || p.walking) return;
    if (!d.says && selected !== d.name && d.place !== 'approval' && d.place !== 'security') return;
    var max = selected === d.name ? 140 : 46;
    if (text.length > max) text = text.slice(0, max - 1) + '…';
    ctx.font = '12px system-ui,sans-serif';
    var w = Math.min(260, ctx.measureText(text).width + 14), x = p.screen[0], y = p.screen[1] - p.screen[2] * 3.4 - 12;
    var lines = wrap(text, w - 14), h = lines.length * 15 + 8;
    ctx.fillStyle = dark ? 'rgba(44,44,46,0.95)' : 'rgba(255,255,255,0.95)';
    ctx.strokeStyle = 'rgba(0,0,0,0.25)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.roundRect(x - w / 2, y - h, w, h, 7); ctx.moveTo(x - 5, y); ctx.lineTo(x, y + 6); ctx.lineTo(x + 5, y); ctx.fill(); ctx.stroke();
    ctx.fillStyle = dark ? '#f2f2f7' : '#1d1d1f'; ctx.textAlign = 'left';
    lines.forEach(function (l, i) { ctx.fillText(l, x - w / 2 + 7, y - h + 16 + i * 15); });
  }
  function wrap(text, w) {
    var words = text.split(' '), out = [], line = '';
    words.forEach(function (word) {
      var t = line ? line + ' ' + word : word;
      if (ctx.measureText(t).width > w && line) { out.push(line); line = word; } else line = t;
    });
    if (line) out.push(line);
    return out.slice(0, 6);
  }
  function draw() {
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = dark ? '#161618' : '#f5f5f7'; ctx.fillRect(0, 0, W, H);
    var floors = [];
    Object.keys(ROOMS).forEach(function (k) {
      var r = ROOMS[k];
      quad([[r.x0, 0, r.z0], [r.x1, 0, r.z0], [r.x1, 0, r.z1], [r.x0, 0, r.z1]], [0, 1, 0], r.floor, floors);
    });
    quad([[17.5, 0.01, 26.5], [22.5, 0.01, 26.5], [22.5, 0.01, 28], [17.5, 0.01, 28]], [0, 1, 0], '#8c5a3c', floors);
    floors.forEach(function (f) { f.draw(); });
    var items = [];
    boxes.forEach(function (b) { addBox(b, items); });
    var t = performance.now() / 600;  // blinking server lights
    for (var i = 0; i < 8; i++) {
      var rx = 10.7 + (i % 4) * 1.8, rz = i < 4 ? 1.52 : 5.48, on = Math.sin(t + i * 1.7) > 0;
      addBox([rx + 0.2, 0.4 + (i % 3) * 0.5, rz, 0.15, 0.08, 0.02, on ? '#3ddc84' : '#1f5133'], items);
    }
    var you = waitingForYou ? [25.2, 20] : RECEPTION;
    figure(you[0], you[1], '#2f3b52', 'You', 'human', items, null);
    Object.keys(people).forEach(function (n) {
      var p = people[n];
      figure(p.x, p.z, p.color, p.data.name.length > 18 ? p.data.name.slice(0, 17) + '…' : p.data.name,
             p.data.kind, items, p);
    });
    items.sort(function (a, b) { return b.z - a.z; });
    items.forEach(function (it) { it.draw(); });
    ctx.textAlign = 'center'; ctx.font = '600 12px system-ui,sans-serif';
    Object.keys(ROOMS).forEach(function (k) {  // room signs hang above each room
      var r = ROOMS[k], c = view((r.x0 + r.x1) / 2, 2.6, (r.z0 + r.z1) / 2);
      if (c[2] <= 0.5) return;
      var p = proj(c), w = ctx.measureText(r.label).width + 12;
      ctx.fillStyle = dark ? 'rgba(30,30,32,0.75)' : 'rgba(255,255,255,0.75)'; ctx.fillRect(p[0] - w / 2, p[1] - 13, w, 18);
      ctx.fillStyle = dark ? '#d1d1d6' : '#3a3a3c'; ctx.fillText(r.label, p[0], p[1]);
    });
    Object.keys(people).forEach(function (n) { bubble(people[n]); });
    if (selected && people[selected] && people[selected].screen) {
      var s = people[selected].screen; ctx.strokeStyle = '#0a84ff'; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(s[0], s[1], s[2] + 4, 0, 7); ctx.stroke();
    }
  }
  var last = performance.now();
  function frame(now) { step(Math.min(0.1, (now - last) / 1000)); last = now; draw(); requestAnimationFrame(frame); }

  // Turn, tilt and zoom.
  var drag = null;
  canvas.addEventListener('pointerdown', function (e) { drag = {x: e.clientX, y: e.clientY, moved: false}; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointermove', function (e) {
    if (!drag) return;
    var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    cam.yaw += dx * 0.008; cam.pitch = Math.max(0.25, Math.min(1.45, cam.pitch + dy * 0.006));
    drag.x = e.clientX; drag.y = e.clientY;
  });
  canvas.addEventListener('pointerup', function (e) {
    if (drag && !drag.moved) {
      var r = canvas.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top, best = null, bd = 30;
      Object.keys(people).forEach(function (n) {
        var s = people[n].screen; if (!s) return;
        var d = Math.hypot(s[0] - mx, s[1] - my); if (d < Math.max(bd, s[2] * 2) && d < bd + s[2]) { bd = d; best = n; }
      });
      selected = best; showInfo();
    }
    drag = null;
  });
  canvas.addEventListener('wheel', function (e) { e.preventDefault(); cam.dist = Math.max(14, Math.min(80, cam.dist * (e.deltaY > 0 ? 1.1 : 0.9))); }, {passive: false});
  document.getElementById('office-reset').addEventListener('click', function () { cam.yaw = 2.55; cam.pitch = 0.9; cam.dist = 54; });
  window.addEventListener('resize', resize);

  function poll() {
    fetch('/office.json', {credentials: 'same-origin', cache: 'no-store'})
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(update)
      .catch(function () { stamp.textContent = 'cannot reach the console'; })
      .then(function () { setTimeout(poll, 3000); });
  }
  resize(); poll(); requestAnimationFrame(frame);
})();
"""

STYLE = """
.office{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:12px}
.office canvas{width:100%;height:68vh;min-height:420px;border:1px solid var(--line);border-radius:8px;touch-action:none;cursor:grab}
.office aside{border:1px solid var(--line);border-radius:8px;padding:10px;max-height:68vh;overflow:auto}
.office h3{font-size:12px;color:var(--muted);margin:10px 0 4px}.office .who{display:block;width:100%;text-align:left;margin:2px 0}
.office .who.on{outline:2px solid #0a84ff}.office pre{white-space:pre-wrap;font:12px/1.4 system-ui,sans-serif;margin:0 0 8px}
@media (max-width:800px){.office{grid-template-columns:1fr}}
"""


def page(query, esc) -> str:
    """The Office page. Without JavaScript the table below the canvas still says where everyone is."""
    st = state(query)
    rows = "".join(f"<tr><td>{esc(a['name'])}</td><td>{esc(PLACE_NAMES[a['place']])}</td><td>{esc(a['state'])}</td></tr>"
                   for a in st["agents"])
    return (f"<style>{STYLE}</style>"
            '<p class="muted">Each agent walks to the place that matches what it is doing now: desks for work, '
            "the library for memory and the web, the server room for code, the talk lounge when two agents talk, "
            "the meeting room for a discussion of three or more, the lobby to wait for your approval, the security "
            'office when held or refused, the break room when idle. <span id="office-ts"></span> '
            '<button type="button" id="office-reset">Reset view</button></p>'
            '<div class="office"><canvas id="office" aria-label="3D office of agents"></canvas>'
            '<aside><pre id="office-info"></pre><div id="office-list"></div></aside></div>'
            f'<noscript><table><thead><tr><th>Agent</th><th>Place</th><th>Doing</th></tr></thead><tbody>{rows}'
            "</tbody></table></noscript>"
            '<script src="/office.js"></script>')
