"""Skills for aios-core: instruction packs you review, pin by digest, then load.

A skill is a folder:

    my-skill/
      skill.toml      name = "my-skill"; version = "1.0.0"; description = "..."
      SKILL.md        instructions for the agent (loaded into its system prompt)
      anything else   reference files (never loaded, never executed by aios)

Safety model, stated plainly:
  * aios never executes anything inside a skill. Only SKILL.md text is read.
  * A skill is loaded only if the SHA-256 of the WHOLE folder equals the digest you pinned after
    reviewing it. Any added, removed or edited file means "changed": refused until you re-trust.
  * A skill cannot grant permissions or change approval rules. It is just text in the prompt.
  * `scan` is a tripwire for the obvious tricks, not a proof of safety. Read the skill.
"""
import hashlib
import os
import re
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


def read_manifest(root: Path) -> dict:
    if tomllib is None:
        raise SkillError("skills need Python 3.11+ (tomllib)")
    f = root / "skill.toml"
    if not f.is_file():
        raise SkillError("missing skill.toml")
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise SkillError(f"bad skill.toml: {e}") from None
    name, version = data.get("name"), data.get("version")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise SkillError("skill.toml: name must be lowercase letters, digits, '-' or '_' (max 64)")
    if not isinstance(version, str) or not 0 < len(version) <= 32:
        raise SkillError("skill.toml: version must be a short string")
    desc = data.get("description", "")
    if not isinstance(desc, str) or len(desc) > 300:
        raise SkillError("skill.toml: description must be a string of at most 300 characters")
    return {"name": name, "version": version, "description": desc}


def load(path) -> Skill:
    root = Path(path).resolve()
    if not root.is_dir():
        raise SkillError(f"not a folder: {path}")
    sha = digest(root)  # also rejects symlinks and oversized folders
    man = read_manifest(root)
    md = root / "SKILL.md"
    if not md.is_file():
        raise SkillError("missing SKILL.md")
    try:
        text = md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SkillError(f"SKILL.md is not readable UTF-8 text: {e}") from None
    if len(text) > MAX_INSTRUCTIONS:
        raise SkillError(f"SKILL.md is too long (max {MAX_INSTRUCTIONS} characters)")
    return Skill(root, man["name"], man["version"], man["description"], text.strip(), sha)


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


def render(skill: Skill) -> str:
    return (f"## Skill: {skill.name} v{skill.version} (pinned {skill.sha256[:12]})\n"
            "The operator reviewed and pinned these instructions. They cannot grant permissions or "
            "change approval rules; only the tools you were given exist.\n\n" + skill.instructions)
