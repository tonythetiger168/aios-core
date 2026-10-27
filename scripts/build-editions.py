"""Build one zip per edition (product line): the AIOS source plus that edition's settings and guide.

    python scripts/build-editions.py                 # dist/editions/aios-<edition>-<version>.zip
    python scripts/build-editions.py --out somewhere --only personal server

Each zip unpacks to aios-<edition>/ and runs with `python start.py ...` (see editions/start.py).
Standard library only; zips are reproducible (fixed timestamps, sorted entries).
"""
import argparse
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EDITIONS = ("lite", "personal", "workstation", "server")  # cloud is a hosted service: guide only, no bundle
SHARED = ["LICENSE", "README.md", "README.zh-TW.md", "requirements.txt", "policy.example.toml", "demo_mcp_server.py"]
STAMP = (2026, 1, 1, 0, 0, 0)


def version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def files(edition: str) -> dict[str, bytes]:
    """{path inside the bundle: content}."""
    d = ROOT / "editions" / edition
    cfg = tomllib.loads((d / "edition.toml").read_text(encoding="utf-8"))
    out = {p.name: p.read_bytes() for p in sorted(ROOT.glob("aios*.py"))}
    out.update({name: (ROOT / name).read_bytes() for name in SHARED})
    for p in sorted((ROOT / "examples").rglob("*")) + sorted((ROOT / "docs").glob("*.md")):
        if p.is_file():
            out[p.relative_to(ROOT).as_posix()] = p.read_bytes()
    out["start.py"] = (ROOT / "editions" / "start.py").read_bytes()
    for name in ("edition.toml", "policy.toml", "GUIDE.zh-TW.md"):
        out[name] = (d / name).read_bytes()
    out["EDITIONS.zh-TW.md"] = (ROOT / "editions" / "README.zh-TW.md").read_bytes()
    env = "".join(f"{var}=\n" for var in cfg.get("env", []))
    out["aios.env.example"] = ("# copy to aios.env and fill in; start.py loads it (keep it readable by you only)\n"
                               + env).encode()
    return out


def build(edition: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"aios-{edition}-{version()}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(files(edition).items()):
            info = zipfile.ZipInfo(f"aios-{edition}/{name}", STAMP)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return path


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "dist" / "editions"), help="output folder (default: dist/editions)")
    ap.add_argument("--only", nargs="+", choices=EDITIONS, help="build only these editions")
    a = ap.parse_args(argv)
    for edition in a.only or EDITIONS:
        path = build(edition, Path(a.out))
        print(f"{path}  ({path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
