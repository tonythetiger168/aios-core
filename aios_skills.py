"""Skills for aios-core: instruction packs you review, pin by digest, then load.

A skill is a folder in the Agent Skills format (agentskills.io), with an optional skill.toml:

    my-skill/
      SKILL.md        ---
                      name: my-skill
                      description: what it does and when to use it
                      ---
                      instructions for the agent (loaded into its system prompt)
      skill.toml      optional: name = "my-skill"; version = "1.0.0"; description = "..."
      anything else   reference files and scripts (never loaded, never executed by aios)

If skill.toml exists it wins, and a different `name` in the SKILL.md front matter is refused.

Safety model, stated plainly:
  * aios never executes anything inside a skill. Only SKILL.md text is read.
  * A skill is loaded only if the SHA-256 of the WHOLE folder equals the digest you pinned after
    reviewing it. Any added, removed or edited file means "changed": refused until you re-trust.
  * A skill cannot grant permissions or change approval rules. It is just text in the prompt.
  * `scan` is a tripwire for the obvious tricks, not a proof of safety. Read the skill.

Publisher signatures (optional): `aios skills sign` writes SKILL.sig, an OpenSSH signature
(`ssh-keygen -Y sign`, namespace "aios-skill") over the skill's name, version and digest. SKILL.sig
itself is left out of the digest. `scan`/`trust` check it against an allowed-signers file (the
format git uses for SSH signing) and say who signed it. A signature tells you who published this
exact content; it is not a review, so trusting still needs your confirmation.

Registry (optional): a static folder or HTTPS site with index.json and one .tar.gz per published
version. `publish` adds a skill to a registry folder; `search` and `install` read one. The index
is not signed: it only helps you find a skill and catches corrupted downloads. Who published it is
answered by SKILL.sig and your allowed signers, and installing still goes through the same scan,
signature check and typed confirmation as `trust`.
"""
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_FILES = 200
MAX_TOTAL_BYTES = 5_000_000
MAX_INSTRUCTIONS = 20_000
MAX_DESCRIPTION = 1024  # the Agent Skills limit for SKILL.md; skill.toml keeps its 300
SIG_FILE = "SKILL.sig"  # at the skill root; not part of the digest it signs
SIG_NAMESPACE = "aios-skill"
SIG_MAX = 16_384
CODE_EXT = {".py", ".sh", ".bash", ".ps1", ".bat", ".cmd", ".js", ".ts", ".rb", ".pl", ".php"}


class SkillError(Exception):
    pass


@dataclass(frozen=True)
class Finding:
    severity: str  # "high" | "medium" | "info"
    file: str
    line: int
    rule: str
    text: str


@dataclass
class Skill:
    path: Path
    name: str
    version: str
    description: str
    instructions: str
    sha256: str


# ---- reading a skill ----
def _files(root: Path) -> list[Path]:
    out, total = [], 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if Path(dirpath) == root:
            filenames = [f for f in filenames if f != SIG_FILE]
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in dirnames + filenames:
            if (Path(dirpath) / name).is_symlink():
                rel = (Path(dirpath) / name).relative_to(root).as_posix()
                raise SkillError(f"symlinks are not allowed ({rel})")
        for name in sorted(filenames):
            p = Path(dirpath) / name
            out.append(p)
            total += p.stat().st_size
        if len(out) > MAX_FILES:
            raise SkillError(f"too many files (max {MAX_FILES})")
        if total > MAX_TOTAL_BYTES:
            raise SkillError(f"skill is too large (max {MAX_TOTAL_BYTES} bytes)")
    return out


def digest(root: Path) -> str:
    """SHA-256 over every file's path and content. CRLF is normalised in text files so the same
    skill checked out on Windows and Linux has the same digest."""
    h = hashlib.sha256()
    for p in _files(root):
        data = p.read_bytes()
        if b"\0" not in data:
            data = data.replace(b"\r\n", b"\n")
        h.update(p.relative_to(root).as_posix().encode("utf-8") + b"\0"
                 + hashlib.sha256(data).hexdigest().encode("ascii") + b"\0")
    return h.hexdigest()


def split_front_matter(text: str) -> tuple[dict, str]:
    """SKILL.md front matter -> (fields, body). Only the YAML subset skills use: `key: value`,
    quoted values, `>`/`|` blocks and one nested map (`metadata:`). No YAML library needed."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise SkillError("SKILL.md: front matter has no closing '---'") from None
    fields: dict = {}
    block = parent = None  # key of an open >/| block; key of an open nested map
    for raw in lines[1:end]:
        indented = raw[:1] in (" ", "\t")
        if block and (indented or not raw.strip()):
            fields[block].append(raw.strip())
            continue
        block = None
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*)$", raw)
        if not m:
            if indented:
                continue  # list items and deeper nesting: not needed here
            raise SkillError(f"SKILL.md: cannot read front matter line: {raw.strip()[:60]}")
        key, value = m.group(1), m.group(2).strip()
        if indented and parent:
            fields[parent][key] = _scalar(value)
            continue
        if indented:
            continue
        parent = None
        if value in (">", "|", ">-", "|-", ">+", "|+"):
            fields[key], block = [], key
        elif value == "":
            fields[key], parent = {}, key
        else:
            fields[key] = _scalar(value)
    for k, v in fields.items():
        if isinstance(v, list):
            fields[k] = " ".join(x for x in v if x)
    return fields, "\n".join(lines[end + 1:])


def _scalar(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return re.sub(r"\s+#.*$", "", v)


def read_manifest(root: Path, front: dict) -> dict:
    f = root / "skill.toml"
    if f.is_file():
        if tomllib is None:
            raise SkillError("skill.toml needs Python 3.11+ (tomllib); remove it to use SKILL.md front matter")
        try:
            data = tomllib.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            raise SkillError(f"bad skill.toml: {e}") from None
        src, max_desc, need_version = "skill.toml", 300, True
        if "name" in front and front["name"] != data.get("name"):
            raise SkillError(f"skill.toml name {data.get('name')!r} differs from SKILL.md name {front['name']!r}")
    elif front:
        meta = front.get("metadata") if isinstance(front.get("metadata"), dict) else {}
        data = {"name": front.get("name"), "version": front.get("version", meta.get("version", "")),
                "description": front.get("description", "")}
        src, max_desc, need_version = "SKILL.md", MAX_DESCRIPTION, False
    else:
        raise SkillError("missing skill.toml or SKILL.md front matter (---, name:, description:, ---)")
    name, version = data.get("name"), data.get("version", "")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise SkillError(f"{src}: name must be lowercase letters, digits, '-' or '_' (max 64)")
    if not isinstance(version, str) or len(version) > 32 or (need_version and not version):
        raise SkillError(f"{src}: version must be a short string")
    desc = data.get("description", "")
    if not isinstance(desc, str) or len(desc) > max_desc:
        raise SkillError(f"{src}: description must be a string of at most {max_desc} characters")
    return {"name": name, "version": version, "description": desc}


def load(path) -> Skill:
    root = Path(path).resolve()
    if not root.is_dir():
        raise SkillError(f"not a folder: {path}")
    sha = digest(root)  # also rejects symlinks and oversized folders
    md = root / "SKILL.md"
    if not md.is_file():
        raise SkillError("missing SKILL.md")
    try:
        text = md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SkillError(f"SKILL.md is not readable UTF-8 text: {e}") from None
    if len(text) > MAX_INSTRUCTIONS:
        raise SkillError(f"SKILL.md is too long (max {MAX_INSTRUCTIONS} characters)")
    front, body = split_front_matter(text)
    man = read_manifest(root, front)
    return Skill(root, man["name"], man["version"], man["description"], body.strip(), sha)


# ---- scanning ----
_I = re.IGNORECASE
# (severity, rule, regex, applies to: "any" | "code" | "doc")
RULES = [
    ("high", "prompt-injection", re.compile(
        r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier|system)\s+(instructions|rules|prompts?)", _I), "any"),
    ("high", "prompt-injection", re.compile(r"disregard\s+(all\s+|the\s+)?(previous|prior|above|system)", _I), "any"),
    ("high", "prompt-injection", re.compile(
        r"override\s+(your|the)\s+(rules|instructions|safety|guidelines|restrictions)", _I), "any"),
    ("high", "hides-from-user", re.compile(
        r"(do\s+not|don'?t|never)\s+(tell|inform|let\s+know|reveal\s+(this\s+)?to)\s+the\s+user", _I), "any"),
    ("high", "prompt-injection", re.compile(r"忽[略视視]\S{0,6}(指令|指示|规则|規則|提示)"), "any"),
    ("high", "hides-from-user", re.compile(r"不要(告訴|告诉)\S{0,4}(使用者|用户|用戶)"), "any"),
    ("high", "skips-approval", re.compile(
        r"(run|execute|write|delete|send|upload|install)\b[^.\n]{0,60}without\s+"
        r"(asking|confirm\w*|approval|permission)", _I), "any"),
    ("high", "weakens-safety", re.compile(
        r"auto-?approve|disable\s+(the\s+)?(approval|audit\w*|logging|sandbox)|"
        r"turn\s+off\s+(the\s+)?(approval|audit\w*|sandbox)", _I), "any"),
    ("high", "exfiltration", re.compile(
        r"(send|post|upload|forward|exfiltrat\w+)\b[^.\n]{0,60}"
        r"(api[_ -]?keys?|passwords?|credentials|secrets?|\.env\b|tokens?)", _I), "any"),
    ("high", "pipe-to-shell", re.compile(
        r"(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh|python3?|pwsh|powershell)\b", _I), "any"),
    ("high", "pipe-to-shell", re.compile(r"\b(iex|Invoke-Expression)\b", _I), "any"),
    ("high", "secret-files", re.compile(
        r"(~|\$HOME|%USERPROFILE%)[/\\]\.?(ssh|aws|gnupg)\b|id_rsa|id_ed25519|/etc/(passwd|shadow)|\.aws/credentials", _I), "any"),
    ("medium", "secret-names", re.compile(
        r"\b(ANTHROPIC|OPENAI|AWS_SECRET|GITHUB)_?[A-Z_]*(KEY|TOKEN)\b|\bAPI_KEY\b"), "any"),
    ("medium", "obfuscation", re.compile(r"[A-Za-z0-9+/]{200,}={0,2}"), "any"),
    ("medium", "network-code", re.compile(
        r"\b(requests|urllib|httpx|aiohttp|socket|websockets?)\b|\bfetch\(|XMLHttpRequest|"
        r"\b(curl|wget)\b|Invoke-WebRequest|Invoke-RestMethod", _I), "code"),
    ("medium", "process-code", re.compile(
        r"\b(eval|exec)\s*\(|os\.system|subprocess|child_process|\bPopen\b|Runtime\.getRuntime|\bspawn\(", _I), "code"),
    ("medium", "obfuscation", re.compile(
        r"base64\.(b64decode|decodebytes)|\batob\(|fromCharCode|(\\x[0-9a-f]{2}){8,}", _I), "code"),
    ("medium", "image-exfil", re.compile(r"!\[[^\]]*\]\(https?://[^)\s]*\?[^)\s]*=[^)]*\)"), "doc"),
]
# zero-width, bidi controls, invisible "tag" characters: text the reader cannot see
_HIDDEN = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")


def scan(path) -> list[Finding]:
    root = Path(path).resolve()
    try:
        load(root)  # structure, manifest, symlinks, size limits
    except SkillError as e:
        return [Finding("high", "", 0, "invalid-skill", str(e))]
    found: list[Finding] = []
    for p in _files(root):
        rel = p.relative_to(root).as_posix()
        is_code = p.suffix.lower() in CODE_EXT
        data = p.read_bytes()
        if b"\0" in data:
            found.append(Finding("info", rel, 0, "binary-file", "binary file (not scanned)"))
            continue
        if is_code:
            found.append(Finding("info", rel, 0, "contains-code",
                                 "code file: aios never runs skill code; review before running it yourself"))
        for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            shown = line.strip()[:120]
            if _HIDDEN.search(line.lstrip("\ufeff") if n == 1 else line):
                found.append(Finding("high", rel, n, "hidden-characters", shown))
            for sev, rule, rx, scope in RULES:
                if (scope == "code" and not is_code) or (scope == "doc" and is_code):
                    continue
                if rx.search(line):
                    found.append(Finding(sev, rel, n, rule, shown))
    order = {"high": 0, "medium": 1, "info": 2}
    return sorted(found, key=lambda f: (order[f.severity], f.file, f.line))


def has_high(findings) -> bool:
    return any(f.severity == "high" for f in findings)


# ---- trust ----
def trust_state(store, skill: Skill) -> tuple[str, str]:
    """('trusted' | 'untrusted' | 'changed', explanation)."""
    pin = store.get_skill_pin(skill.name)
    if not pin:
        return "untrusted", f"it has not been reviewed and pinned: aios skills scan/trust {skill.path}"
    if pin["sha256"] != skill.sha256:
        return "changed", (f"its content changed since you trusted it (pinned {pin['sha256'][:12]}, "
                           f"now {skill.sha256[:12]}): review it again, then re-trust")
    return "trusted", ""


def label(name: str, version: str) -> str:
    return f"{name} v{version}" if version else name


def render(skill: Skill) -> str:
    return (f"## Skill: {label(skill.name, skill.version)} (pinned {skill.sha256[:12]})\n"
            "The operator reviewed and pinned these instructions. They cannot grant permissions or "
            "change approval rules; only the tools you were given exist.\n\n" + skill.instructions)


# ---- publisher signatures (OpenSSH SSHSIG via ssh-keygen) ----
def statement(skill: Skill) -> bytes:
    """What a publisher signs: the skill's identity and the digest of all its files."""
    return (f"aios-skill-v1\nname={skill.name}\nversion={skill.version}\nsha256={skill.sha256}\n").encode()


def ssh_keygen() -> str | None:
    return shutil.which("ssh-keygen")


def sign(path, key: str, keygen: str | None = None) -> Path:
    """Write SKILL.sig for the skill at `path` with the SSH private key `key` (ssh-keygen may ask
    for its passphrase). -> the signature file."""
    skill = load(path)
    exe = keygen or ssh_keygen()
    if not exe:
        raise SkillError("ssh-keygen (OpenSSH 8.1 or newer) is needed to sign skills")
    with tempfile.TemporaryDirectory() as tmp:
        msg = Path(tmp) / "statement"
        msg.write_bytes(statement(skill))
        r = subprocess.run([exe, "-Y", "sign", "-q", "-f", str(key), "-n", SIG_NAMESPACE, str(msg)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)
        sig = msg.with_name("statement.sig")
        if r.returncode or not sig.exists():
            raise SkillError(f"signing failed: {(r.stderr or r.stdout).strip()[:300]}")
        out = skill.path / SIG_FILE
        out.write_bytes(sig.read_bytes())
    if load(path).sha256 != skill.sha256:  # the signature must not change what it signs
        raise SkillError("internal error: SKILL.sig changed the digest")
    return out


def verify(skill: Skill, signers, keygen: str | None = None) -> tuple[str, str]:
    """-> (state, detail). state: 'unsigned', 'valid' (detail: the signer), 'unknown-signer',
    'invalid' or 'unavailable' (no ssh-keygen / no allowed-signers file)."""
    sig = skill.path / SIG_FILE
    if sig.is_symlink():
        return "invalid", "SKILL.sig must not be a symlink"
    if not sig.is_file():
        return "unsigned", "no SKILL.sig"
    if sig.stat().st_size > SIG_MAX:
        return "invalid", "SKILL.sig is too large"
    exe = keygen or ssh_keygen()
    if not exe:
        return "unavailable", "ssh-keygen is not installed, so the signature cannot be checked"
    signers = Path(signers) if signers else None
    if not signers or not signers.is_file():
        return "unavailable", f"no allowed-signers file ({signers}), so the signer is unknown"
    try:
        found = subprocess.run([exe, "-Y", "find-principals", "-s", str(sig), "-f", str(signers)],
                               capture_output=True, text=True, timeout=30)
        principals = [p.strip() for p in found.stdout.splitlines() if p.strip()] if found.returncode == 0 else []
        if not principals:
            return "unknown-signer", "signed by a key that is not in your allowed signers"
        for who in principals:
            r = subprocess.run([exe, "-Y", "verify", "-f", str(signers), "-I", who, "-n", SIG_NAMESPACE,
                                "-s", str(sig)], input=statement(skill), capture_output=True, timeout=30)
            if r.returncode == 0:
                return "valid", who
    except (OSError, subprocess.TimeoutExpired) as e:
        return "unavailable", f"ssh-keygen failed: {e}"
    return "invalid", "the signature does not match this content (changed after signing?)"


# ---- registry: a static index.json plus archives, served from a folder or over HTTPS ----
REGISTRY_FORMAT = "aios-skill-registry-1"
PUBLIC_REGISTRY = "https://aios-registry.pages.dev/"  # built from registry/ in this repo, served by Cloudflare Pages
INDEX_FILE = "index.json"
MAX_ARCHIVE = 8_000_000
MAX_INDEX = 4_000_000
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ARCHIVE_RE = re.compile(r"^skills/[a-z0-9][a-z0-9_-]{0,63}/[A-Za-z0-9._+-]{1,120}\.tar\.gz$")


def pack(path) -> bytes:
    """A deterministic .tar.gz of the skill (its files plus SKILL.sig), the same bytes every time."""
    import gzip
    import io
    import tarfile
    skill = load(path)
    files = _files(skill.path)
    sig = skill.path / SIG_FILE
    if sig.is_file() and not sig.is_symlink():
        files.append(sig)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for p in sorted(files, key=lambda p: p.relative_to(skill.path).as_posix()):
            data = p.read_bytes()
            info = tarfile.TarInfo(p.relative_to(skill.path).as_posix())
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def unpack(data: bytes, dest) -> Path:
    """Extract a skill archive into the new folder `dest`. Only plain files with plain relative
    names are accepted (no links, devices, absolute paths or '..'), within the skill size limits."""
    import io
    import tarfile
    dest = Path(dest)
    dest.mkdir(parents=True)
    count, total = 0, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for m in tar:
                name = m.name
                parts = name.split("/")
                if (not name or name.startswith("/") or "\\" in name or ":" in name
                        or any(p in ("", ".", "..") for p in parts)):
                    raise SkillError(f"archive has an unsafe path: {name[:100]!r}")
                if m.isdir():
                    continue
                if not m.isreg():
                    raise SkillError(f"archive may hold only plain files ({name[:100]})")
                count, total = count + 1, total + m.size
                if count > MAX_FILES + 1 or total > MAX_TOTAL_BYTES + SIG_MAX:
                    raise SkillError("archive is larger than a skill may be")
                out = dest.joinpath(*parts)
                if out.exists():
                    raise SkillError(f"archive lists {name[:100]} twice")
                out.parent.mkdir(parents=True, exist_ok=True)
                f = tar.extractfile(m)
                out.write_bytes(f.read(m.size))
                if out.stat().st_size != m.size:
                    raise SkillError(f"archive member {name[:100]} is truncated")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise SkillError(f"not a valid skill archive: {e}") from None
    return dest


def _location(registry: str) -> tuple[str, str]:
    """-> ('dir', path) or ('url', base URL ending in '/')."""
    from urllib.parse import urlsplit
    if not registry:
        raise SkillError("no registry: pass --registry or set AIOS_SKILL_REGISTRY")
    u = urlsplit(registry)
    if u.scheme == "file":
        from urllib.request import url2pathname
        return "dir", url2pathname(u.path)
    if u.scheme in ("http", "https"):
        if u.scheme == "http" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise SkillError("a remote registry must use https:// (plain http only for localhost)")
        return "url", registry if registry.endswith("/") else registry + "/"
    if u.scheme and len(u.scheme) > 1:
        raise SkillError(f"unsupported registry location: {registry}")
    return "dir", registry


def _get(registry: str, rel: str, limit: int) -> bytes:
    kind, where = _location(registry)
    if kind == "dir":
        p = Path(where) / rel
        try:
            if p.stat().st_size > limit:
                raise SkillError(f"{rel} is too large (max {limit} bytes)")
            return p.read_bytes()
        except OSError as e:
            raise SkillError(f"cannot read {rel} from the registry: {e}") from None
    from urllib.error import URLError
    from urllib.parse import urljoin
    from urllib.request import Request, urlopen
    try:
        with urlopen(Request(urljoin(where, rel), headers={"User-Agent": "aios-skills"}), timeout=30) as r:
            data = r.read(limit + 1)
    except (URLError, OSError, ValueError) as e:
        raise SkillError(f"cannot fetch {rel} from the registry: {e}") from None
    if len(data) > limit:
        raise SkillError(f"{rel} is too large (max {limit} bytes)")
    return data


def _check_entry(e) -> dict:
    if not isinstance(e, dict):
        raise SkillError("registry index has a malformed entry")
    out = {k: e.get(k) for k in ("name", "version", "description", "sha256", "archive", "archive_sha256", "signed")}
    if not (isinstance(out["name"], str) and NAME_RE.match(out["name"])
            and isinstance(out["version"], str) and len(out["version"]) <= 40
            and isinstance(out["sha256"], str) and _HEX64.match(out["sha256"])
            and isinstance(out["archive_sha256"], str) and _HEX64.match(out["archive_sha256"])
            and isinstance(out["archive"], str) and _ARCHIVE_RE.match(out["archive"])
            and ".." not in out["archive"]):
        raise SkillError(f"registry index has a malformed entry ({str(e.get('name'))[:64]})")
    out["description"] = out["description"] if isinstance(out["description"], str) else ""
    out["signed"] = out["signed"] is True
    return out


def read_index(registry: str) -> list[dict]:
    import json
    try:
        doc = json.loads(_get(registry, INDEX_FILE, MAX_INDEX).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise SkillError(f"registry index is not valid JSON: {e}") from None
    if not isinstance(doc, dict) or doc.get("format") != REGISTRY_FORMAT or not isinstance(doc.get("skills"), list):
        raise SkillError(f"not an aios skill registry (expected format {REGISTRY_FORMAT!r})")
    return [_check_entry(e) for e in doc["skills"]]


def search(registry: str, query: str = "") -> list[dict]:
    """Newest entry per skill name, matching `query` in the name or description."""
    latest = {}
    for e in read_index(registry):
        latest[e["name"]] = e  # later entries were published later
    q = query.lower()
    return [e for e in latest.values() if q in e["name"] or q in e["description"].lower()]


def find(registry: str, spec: str) -> dict:
    """`name` (newest published) or `name@version`."""
    name, _, version = spec.partition("@")
    hits = [e for e in read_index(registry) if e["name"] == name and (not version or e["version"] == version)]
    if not hits:
        raise SkillError(f"{spec} is not in the registry")
    return hits[-1]


def fetch(registry: str, entry: dict, dest) -> Skill:
    """Download `entry`, check it against the index, unpack it into the new folder `dest`."""
    data = _get(registry, entry["archive"], MAX_ARCHIVE)
    if hashlib.sha256(data).hexdigest() != entry["archive_sha256"]:
        raise SkillError("the archive does not match the registry index (corrupted or replaced)")
    skill = load(unpack(data, dest))
    if (skill.name, skill.version, skill.sha256) != (entry["name"], entry["version"], entry["sha256"]):
        raise SkillError("the archive's content does not match what the registry index says it is")
    return skill


def publish(path, registry_dir) -> dict:
    """Add the skill at `path` to the registry folder `registry_dir` (created if missing). A
    published name and version is immutable: publishing it again with other content is refused."""
    import json
    skill = load(path)
    reg = Path(registry_dir)
    index = reg / INDEX_FILE
    entries = read_index(str(reg)) if index.exists() else []
    for e in entries:
        if e["name"] == skill.name and e["version"] == skill.version:
            if e["sha256"] == skill.sha256:
                return e
            raise SkillError(f"{label(skill.name, skill.version)} is already published with other content; "
                             "bump the version")
    data = pack(path)
    rel = f"skills/{skill.name}/{skill.name}-{skill.version or skill.sha256[:12]}.tar.gz"
    out = reg / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    entry = {"name": skill.name, "version": skill.version, "description": skill.description,
             "sha256": skill.sha256, "archive": rel, "archive_sha256": hashlib.sha256(data).hexdigest(),
             "signed": (skill.path / SIG_FILE).is_file()}
    _check_entry(entry)
    entries.append(entry)
    tmp = index.with_name(INDEX_FILE + ".tmp")
    tmp.write_text(json.dumps({"format": REGISTRY_FORMAT, "skills": entries}, indent=1, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, index)
    return entry
