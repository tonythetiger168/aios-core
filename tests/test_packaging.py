"""pyproject.toml ships every module and the `aios` command."""
import re
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import aios  # noqa: E402


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_every_module_is_packaged(self):
        shipped = sorted(self.meta["tool"]["setuptools"]["py-modules"])
        self.assertEqual(shipped, sorted(p.stem for p in ROOT.glob("aios*.py")))

    def test_the_command_runs_the_cli(self):
        module, _, func = self.meta["project"]["scripts"]["aios"].partition(":")
        self.assertEqual((module, getattr(aios, func)), ("aios", aios.cli))

    def test_no_required_dependencies_and_the_readme_names_the_same_python(self):
        self.assertEqual(self.meta["project"]["dependencies"], [])
        minimum = re.fullmatch(r">=(3\.\d+)", self.meta["project"]["requires-python"]).group(1)
        for readme in ("README.md", "README.zh-TW.md"):
            self.assertIn(f"Python {minimum}", (ROOT / readme).read_text(encoding="utf-8"), readme)


if __name__ == "__main__":
    unittest.main()
