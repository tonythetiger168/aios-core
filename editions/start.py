"""Start AIOS with one edition's settings (model server, sandbox, concurrency, policy).

In a bundle built by scripts/build-editions.py (edition.toml sits next to this file):
    python start.py install            # pip install what this edition needs
    python start.py check              # aios doctor + is the model server reachable
    python start.py chat               # aios chat with the edition's flags and policy.toml
    python start.py daemon | swarm --tasks t.txt | console | jobs list | ...
    python start.py service systemd --home .   # keep the daemon running, with the edition's flags
From the source tree:
    python editions/start.py --edition personal chat

Edition flags come first, so anything you add after the command wins (argparse keeps the last value),
e.g. `python start.py chat --model qwen2.5:7b`. Variables in aios.env (KEY=value) are loaded unless
already set in the environment. Standard library only.
"""
import os
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
EDITIONS = ("lite", "personal", "workstation", "server")
MODEL_CMDS = ("chat", "daemon", "swarm")  # commands that take the model, sandbox and swarm flags
POLICY_CMDS = ("chat", "daemon", "swarm", "serve", "a2a-serve")


def locate(argv: list[str]) -> tuple[Path, Path, list[str]]:
    """(edition folder, folder holding aios.py, remaining argv)."""
    if (HERE / "edition.toml").exists():
        return HERE, HERE, argv
    if len(argv) >= 2 and argv[0] == "--edition":
        name, argv = argv[1], argv[2:]
        if name not in EDITIONS:
            raise SystemExit(f"--edition must be one of {', '.join(EDITIONS)}")
        return HERE / name, HERE.parent, argv
    raise SystemExit("from the source tree: python editions/start.py --edition <name> <command> ...")


def load(edition: Path) -> dict:
    with open(edition / "edition.toml", "rb") as f:
        return tomllib.load(f)


def command(cfg: dict, edition: Path, root: Path, argv: list[str]) -> list[str]:
    """The aios command line for argv (edition flags first, the user's after them)."""
    if not argv:
        raise SystemExit("usage: start.py install | check | <aios command> [args]")
    cmd, rest = argv[0], argv[1:]
    flags = []
    if cmd in MODEL_CMDS:
        flags += cfg["flags"].get("common", []) + cfg["flags"].get(cmd, [])
    if cmd in POLICY_CMDS and "--policy" not in rest:
        flags += ["--policy", str(edition / "policy.toml")]
    if cmd == "service" and "--" not in rest:  # the service runs `aios daemon` with the edition's flags
        rest = [*rest, "--", *cfg["flags"].get("common", []), *cfg["flags"].get("daemon", []),
                "--policy", str(edition / "policy.toml")]
    return [sys.executable, str(root / "aios.py"), cmd, *flags, *rest]


def load_env(edition: Path) -> None:
    path = edition / "aios.env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


def install(cfg: dict) -> int:
    pkgs = cfg.get("pip", [])
    if not pkgs:
        print("nothing to install: this edition uses the standard library only")
        return 0
    return subprocess.call([sys.executable, "-m", "pip", "install", *pkgs])


def flag(flags: list[str], name: str) -> str | None:
    return flags[flags.index(name) + 1] if name in flags else None


def check(cfg: dict, root: Path) -> int:
    bad = 0
    for var in cfg.get("env", []):
        if not os.environ.get(var):
            print(f"missing: ${var} (put it in aios.env or your environment)")
            bad = 1
    common = cfg["flags"].get("common", [])
    if flag(common, "--provider") == "openai":
        url = flag(common, "--base-url") or "http://localhost:11434/v1"
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=5) as r:
                print(f"model server ok: {url} (HTTP {r.status})")
        except Exception as e:  # noqa: BLE001 - any failure means "not reachable"
            print(f"model server not reachable at {url}: {e}")
            bad = 1
    return subprocess.call([sys.executable, str(root / "aios.py"), "doctor"]) or bad


def main(argv: list[str]) -> int:
    edition, root, argv = locate(argv)
    cfg = load(edition)
    load_env(edition)
    if argv[:1] == ["install"]:
        return install(cfg)
    if argv[:1] == ["check"]:
        return check(cfg, root)
    return subprocess.call(command(cfg, edition, root, argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
