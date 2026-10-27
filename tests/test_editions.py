"""Editions (product lines): every edition's flags, policy and bundle actually work with this aios."""
import importlib.util
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import aios  # noqa: E402


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


start = load(ROOT / "editions" / "start.py", "edition_start")
build = load(ROOT / "scripts" / "build-editions.py", "build_editions")


class EditionTests(unittest.TestCase):
    def test_the_same_editions_everywhere(self):
        self.assertEqual(start.EDITIONS, build.EDITIONS)
        folders = sorted(p.name for p in (ROOT / "editions").iterdir() if p.is_dir() and not p.name.startswith("_"))
        self.assertEqual(folders, sorted(start.EDITIONS + ("cloud",)))
        self.assertTrue((ROOT / "editions" / "cloud" / "GUIDE.zh-TW.md").exists())

    def test_every_edition_command_parses(self):
        """The flags in edition.toml are real aios flags, for each command that gets them."""
        for name in start.EDITIONS:
            d = ROOT / "editions" / name
            cfg = start.load(d)
            for argv in (["chat"], ["daemon"], ["swarm", "--tasks", "t.txt"], ["console"],
                         ["service", "systemd"]):
                with self.subTest(edition=name, cmd=argv[0]):
                    cmd = start.command(cfg, d, ROOT, argv)
                    self.assertEqual(cmd[1], str(ROOT / "aios.py"))
                    args = cmd[2:]
                    if argv[0] == "service":  # what follows -- must be a valid daemon command line
                        i = args.index("--")
                        aios.parser().parse_args(args[:i])
                        args = ["daemon", *args[i + 1:]]
                    a = aios.parser().parse_args(args)
                    if argv[0] in ("chat", "daemon", "swarm"):
                        self.assertEqual(a.policy, str(d / "policy.toml"))

    def test_user_flags_come_after_the_edition_flags(self):
        d = ROOT / "editions" / "personal"
        cmd = start.command(start.load(d), d, ROOT, ["chat", "--model", "qwen2.5:7b"])
        self.assertEqual(aios.parser().parse_args(cmd[2:]).model, "qwen2.5:7b")
        cmd = start.command(start.load(d), d, ROOT, ["chat", "--policy", "mine.toml"])
        self.assertEqual(cmd.count("--policy"), 1)

    def test_every_policy_loads_and_every_guide_lists_prices(self):
        for name in start.EDITIONS:
            with self.subTest(edition=name):
                policy = aios.load_policy(str(ROOT / "editions" / name / "policy.toml"))
                self.assertTrue({"main", "reporter", "lead", "worker", "editor"} <= set(policy))
                self.assertEqual(policy["worker"]["permissions"], {"now", "read_file", "list_files"})
        for name in start.EDITIONS + ("cloud",):
            guide = (ROOT / "editions" / name / "GUIDE.zh-TW.md").read_text(encoding="utf-8")
            self.assertIn("## 收費", guide, name)

    def test_bundle_has_the_whole_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build.build("server", Path(tmp))
            with zipfile.ZipFile(path) as z:
                names = {n.removeprefix("aios-server/") for n in z.namelist()}
                cfg = tomllib.loads(z.read("aios-server/edition.toml").decode())
            self.assertEqual(cfg["name"], "server")
            shipped = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
            for mod in shipped["tool"]["setuptools"]["py-modules"]:
                self.assertIn(f"{mod}.py", names)
            for f in ("start.py", "edition.toml", "policy.toml", "GUIDE.zh-TW.md", "EDITIONS.zh-TW.md",
                      "aios.env.example", "LICENSE", "README.zh-TW.md"):
                self.assertIn(f, names)
            again = build.build("server", Path(tmp) / "again")
            self.assertEqual(path.read_bytes(), again.read_bytes())  # reproducible


if __name__ == "__main__":
    unittest.main()
