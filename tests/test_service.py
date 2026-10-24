"""`aios service`: service definitions that keep the daemon running, with no secrets inside."""
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios_service as svc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY, SCRIPT, HOME = "/usr/bin/python3", "/opt/aios/aios.py", "/home/me/aios data"
ARGS = ["--", "--policy", "policy.toml", "--provider", "openai", "--model", "llama3.1"]


class RenderTests(unittest.TestCase):
    def test_systemd_restarts_and_quotes(self):
        out = svc.render("systemd", HOME, ARGS, PY, SCRIPT)
        self.assertIn("Restart=on-failure", out)
        self.assertIn(f"ExecStart={PY} {SCRIPT} daemon --home '{HOME}' --policy policy.toml --provider openai "
                      "--model llama3.1", out)
        self.assertIn(f"EnvironmentFile=-{HOME}/aios.env", out)
        self.assertIn("enable-linger", out)

    def test_systemd_escapes_specifiers_and_variables(self):
        out = svc.render("systemd", "/data/100%", ["--policy", "$HOME/p.toml"], PY, SCRIPT)
        self.assertIn("/data/100%%", out)
        self.assertIn("$$HOME/p.toml", out)

    def test_launchd_keeps_alive_and_escapes_xml(self):
        out = svc.render("launchd", "/Users/me/a&b", ["--policy", "<p>.toml"], PY, SCRIPT)
        self.assertIn("<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>", out)
        self.assertIn("a&amp;b", out)
        self.assertIn("&lt;p&gt;.toml", out)
        self.assertNotIn("<p>.toml", out)
        self.assertIn(". ./aios.env", out)  # keys come from a file, not the plist

    def test_windows_restarts_and_quotes_for_powershell(self):
        out = svc.render("windows", r"C:\Users\o'neil\aios data", ["--tick", "30"], r"C:\Python312\python.exe",
                         r"C:\aios\aios.py")
        self.assertIn("-RestartCount 999", out)
        self.assertIn("'C:\\Users\\o''neil\\aios data'", out)  # single quote doubled inside '...'
        self.assertIn('daemon --home "C:\\Users\\o\'\'neil\\aios data" --tick 30', out)
        with self.assertRaises(ValueError):
            svc.render("windows", "C:\\x", ["--policy", 'a"b'], PY, SCRIPT)

    def test_once_and_home_cannot_be_passed_through(self):
        for bad in (["--once"], ["--home", "/elsewhere"], ["--home=/elsewhere"]):
            for kind in svc.KINDS:
                with self.subTest(kind=kind, bad=bad), self.assertRaises(ValueError):
                    svc.render(kind, HOME, bad, PY, SCRIPT)

    def test_no_secrets_in_any_definition(self):
        for kind in svc.KINDS:
            out = svc.render(kind, HOME, [], PY, SCRIPT)
            self.assertNotIn("sk-ant", out.replace('"sk-ant-..."', ""))  # only the placeholder in the Windows hint
            self.assertNotIn("ANTHROPIC_API_KEY=", out)


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "aios.py"), "service", *args],
                              capture_output=True, text=True, timeout=60)

    def test_flags_after_the_double_dash_go_to_the_daemon(self):
        r = self.run_cli("systemd", "--home", HOME, "--", "--tick", "30", "--policy", "p.toml")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("daemon --home", r.stdout)
        self.assertIn("--tick 30 --policy p.toml", r.stdout)

    def test_refused_flags_are_a_usage_error(self):
        r = self.run_cli("systemd", "--", "--once")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--once", r.stderr)


if __name__ == "__main__":
    unittest.main()
