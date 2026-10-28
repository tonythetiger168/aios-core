"""The public download page must deliver complete, verifiable bundles, not repository links."""
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("product_builder", ROOT / "scripts/build-products.py")
products = importlib.util.module_from_spec(spec)
spec.loader.exec_module(products)


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(v for k, v in attrs if k == "href")


class ProductTests(unittest.TestCase):
    def test_download_journey(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "skills.html").write_text("skill catalog", encoding="utf-8")
            products.build(out)
            page = (out / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("github.com", page)
            self.assertNotIn("{{", page)
            for guide in (out / "guides").glob("*.html"):
                self.assertNotIn("github.com", guide.read_text(encoding="utf-8"), guide.name)
            for plan in ("Core", "Team", "Cloud", "Enterprise", "NT$490", "NT$790"):
                self.assertIn(plan, page)
            links = Links()
            links.feed(page)
            for href in links.hrefs:
                if not href.startswith("#"):
                    self.assertTrue((out / href).is_file(), href)
            records = json.loads((out / "products.json").read_text(encoding="utf-8"))["products"]
            self.assertEqual([r["id"] for r in records], list(products.editions.EDITIONS))
            sums = (out / "downloads/SHA256SUMS.txt").read_text(encoding="utf-8")
            for record in records:
                archive = out / record["file"]
                self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), record["sha256"])
                self.assertIn(record["sha256"], sums)
                with zipfile.ZipFile(archive) as z:
                    self.assertIsNone(z.testzip())
                    z.extractall(out / "unpacked")
                bundle = out / "unpacked" / f'aios-{record["id"]}'
                result = subprocess.run([sys.executable, "start.py", "--help"], cwd=bundle,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
