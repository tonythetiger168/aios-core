#!/usr/bin/env python3
"""Build the public skill registry site from the skill folders in a source directory.

    python scripts/build-registry.py registry _site --live https://aios-registry-47b.pages.dev/ [--key KEY]

The site is the registry format `aios skills search/install` read (index.json plus one .tar.gz per
version), with an index.html for people. What is already published stays published: the build
starts from the live registry (every archive is downloaded and checked against its index), then
publishes each source folder on top, so an old version keeps installing and a published
name@version can never change content (bump the version instead). Only the very first build, when
the live site answers 404, starts empty.

Every skill must scan clean (no high or medium findings). With --key, each skill is signed with
that SSH key first; a signers file next to the skills (allowed_signers) is copied to the site.
"""
import argparse
import hashlib
import html
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import aios_skills as sk  # noqa: E402


def fetch_live(live: str, out: Path) -> int:
    """Copy the live registry into out; 0 entries if it does not exist yet (HTTP 404)."""
    from urllib.error import HTTPError, URLError
    from urllib.parse import urljoin
    from urllib.request import Request, urlopen
    base = live if live.endswith("/") else live + "/"
    if sk._location(base)[0] == "url":
        try:
            with urlopen(Request(urljoin(base, sk.INDEX_FILE), headers={"User-Agent": "aios-skills"}), timeout=30):
                pass
        except HTTPError as e:
            if e.code == 404:
                print(f"no registry at {base} yet: starting empty")
                return 0
            raise sk.SkillError(f"cannot read the live registry: {e}") from None
        except (URLError, OSError) as e:
            raise sk.SkillError(f"cannot read the live registry: {e}") from None
    entries = sk.read_index(base)
    for e in entries:
        data = sk._get(base, e["archive"], sk.MAX_ARCHIVE)
        if hashlib.sha256(data).hexdigest() != e["archive_sha256"]:
            raise sk.SkillError(f"live archive {e['archive']} does not match its index entry")
        (out / e["archive"]).parent.mkdir(parents=True, exist_ok=True)
        (out / e["archive"]).write_bytes(data)
    (out / sk.INDEX_FILE).write_bytes(sk._get(base, sk.INDEX_FILE, sk.MAX_INDEX))
    return len(entries)


def page(entries: list[dict], url: str) -> str:
    latest = {}
    for e in entries:
        latest[e["name"]] = e
    rows = "\n".join(
        f"<tr><td><code>{html.escape(e['name'])}</code></td><td>{html.escape(e['version'] or '-')}</td>"
        f"<td>{'signed' if e['signed'] else 'unsigned'}</td><td>{html.escape(e['description'])}</td>"
        f"<td><code>aios skills install {html.escape(e['name'])} --registry {html.escape(url)}</code></td></tr>"
        for e in sorted(latest.values(), key=lambda e: e["name"]))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIOS skill registry</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ccc;padding:.4rem;text-align:left;vertical-align:top}}
code{{font-size:13px}}</style></head><body>
<h1>AIOS skill registry</h1>
<p><a href="./">AIOS products and downloads</a> · Skills for aios-core: instruction packs an agent
loads into its prompt. aios never runs anything inside a skill, and installing one still shows you a scan
and asks you to confirm before it is pinned. A listing here is not an endorsement: read the skill.</p>
<p>Machine-readable index: <a href="{sk.INDEX_FILE}">{sk.INDEX_FILE}</a>. To add a skill, open a pull request
that adds a folder under <code>registry/</code>.</p>
<table><tr><th>Skill</th><th>Version</th><th>Signature</th><th>What it does</th><th>Install</th></tr>
{rows}
</table></body></html>
"""


def build(src: Path, out: Path, live: str | None, key: str | None, url: str) -> list[dict]:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    if live:
        print(f"carried over {fetch_live(live, out)} published versions from {live}")
    folders = sorted(p for p in src.iterdir() if (p / "SKILL.md").is_file())
    if not folders:
        raise sk.SkillError(f"no skill folders in {src}")
    with tempfile.TemporaryDirectory() as tmp:
        for folder in folders:
            bad = [f for f in sk.scan(folder) if f.severity in ("high", "medium")]
            if bad:
                raise sk.SkillError(f"{folder.name} does not scan clean: " +
                                    "; ".join(f"{f.rule} in {f.file}:{f.line}" for f in bad))
            work = Path(tmp) / folder.name
            shutil.copytree(folder, work)
            (work / sk.SIG_FILE).unlink(missing_ok=True)
            if key:
                sk.sign(work, key)
            e = sk.publish(work, out)
            print(f"{sk.label(e['name'], e['version'])}  {e['sha256'][:12]}  "
                  f"{'signed' if e['signed'] else 'unsigned'}")
    signers = src / "allowed_signers"
    if signers.is_file():
        shutil.copyfile(signers, out / "allowed_signers")
    entries = sk.read_index(str(out))
    (out / "index.html").write_text(page(entries, url), encoding="utf-8")
    (out / ".nojekyll").write_text("")
    return entries


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("src", type=Path, help="folder holding one folder per skill")
    ap.add_argument("out", type=Path, help="site folder to write (replaced)")
    ap.add_argument("--live", help="the published registry to carry over (its URL, or a folder)")
    ap.add_argument("--url", help="the URL the site will be served at (shown in install commands)")
    ap.add_argument("--key", help="SSH private key to sign every skill with")
    a = ap.parse_args(argv)
    try:
        entries = build(a.src, a.out, a.live, a.key, a.url or a.live or str(a.out))
    except sk.SkillError as e:
        print(f"cannot build the registry: {e}", file=sys.stderr)
        return 1
    print(f"{len(entries)} published versions in {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
