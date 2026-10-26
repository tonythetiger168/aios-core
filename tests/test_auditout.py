"""Audit copies outside aios.db: the webhook sink and chain-head anchors (aios_auditout)."""
import contextlib
import http.server
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aios  # noqa: E402
import aios_auditout as ao  # noqa: E402
import aios_watchdog as wd  # noqa: E402


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.st = aios.Store(str(self.home / "aios.db"))
        self.addCleanup(self.st.db.close)
        self.anchors = str(self.home / "anchors.jsonl")

    def log(self, n, start=0):
        for i in range(start, start + n):
            self.st.log_audit("main", "read_file", {"path": f"f{i}"}, "text", "ok")

    def rebuild_chain(self, change_id):
        """What someone with write access to aios.db can do: edit an entry and recompute every hash."""
        db, prev = self.st.db, ""
        db.execute("UPDATE audit SET result='forged' WHERE id=?", (change_id,))
        for rid, ts, agent, tool, args, result, status in db.execute(
                "SELECT id, ts, agent, tool, args, result, status FROM audit ORDER BY id").fetchall():
            prev = wd.audit_hash(prev, ts, agent, tool, args, result, status)
            db.execute("UPDATE audit SET hash=? WHERE id=?", (prev, rid))
        db.commit()


class UrlTests(unittest.TestCase):
    def test_https_anywhere_http_only_to_this_machine(self):
        self.assertIsNone(ao.check_url("https://siem.example.com/aios"))
        self.assertIsNone(ao.check_url("http://127.0.0.1:9000/x"))
        self.assertIsNone(ao.check_url("http://localhost/x"))
        self.assertIsNone(ao.check_url("http://[::1]:8080/"))
        self.assertIn("plain http", ao.check_url("http://siem.example.com/aios"))
        self.assertIsNotNone(ao.check_url("ftp://example.com/"))
        self.assertIsNotNone(ao.check_url("https://user:pw@example.com/"))
        self.assertIsNotNone(ao.check_url("not a url"))


class ShipTests(Case):
    def test_batches_go_out_in_order_with_secrets_redacted_and_the_position_is_kept(self):
        self.st.log_audit("main", "fetch", {"url": "https://x", "api_key": "sk-1", "opts": {"Password": "p"}},
                          "ok", "ok")
        self.log(4)
        sent = []
        post = lambda url, body, timeout: sent.append(json.loads(body))
        r = ao.ship_once(self.st.db, "https://c/x", batch=3, post=post)
        self.assertEqual((r["sent"], r["left"], r["error"]), (3, 2, None))
        first = sent[0]["entries"][0]
        self.assertEqual(first["args"], {"url": "https://x", "api_key": "[redacted]", "opts": {"Password": "[redacted]"}})
        self.assertEqual(sent[0]["source"], "aios")
        self.assertTrue(first["hash"])
        r = ao.ship_once(self.st.db, "https://c/x", batch=3, post=post)
        self.assertEqual((r["sent"], r["left"]), (2, 0))
        self.assertEqual([e["id"] for b in sent for e in b["entries"]], [1, 2, 3, 4, 5])
        self.assertEqual(ao.ship_once(self.st.db, "https://c/x", post=post)["sent"], 0)
        # a second sink starts from the beginning on its own
        self.assertEqual(ao.ship_once(self.st.db, "https://other/x", post=post)["sent"], 5)

    def test_a_failure_drops_nothing_and_the_watchdog_raises_after_a_while(self):
        self.log(3)

        def down(url, body, timeout):
            raise OSError("connection refused")
        t0 = datetime(2026, 10, 26, 9, 0)
        r = ao.ship_once(self.st.db, "https://c/x", post=down, now=t0)
        self.assertEqual((r["sent"], r["left"]), (0, 3))
        self.assertIn("connection refused", r["error"])
        self.assertEqual(ao.failing(self.st.db, t0 + timedelta(minutes=5)), [])
        ao.ship_once(self.st.db, "https://c/x", post=down, now=t0 + timedelta(minutes=10))
        [s] = ao.status(self.st.db)
        self.assertEqual((s["behind"], s["error_since"]), (3, wd.iso(t0)))  # the first failure counts
        alerts = wd.check(self.st.db, t0 + timedelta(minutes=16))
        self.assertEqual([(a["rule"], a["subject"]) for a in alerts], [("audit-sink-failing", "https://c/x")])
        got = []
        r = ao.ship_once(self.st.db, "https://c/x", post=lambda u, b, t: got.append(b))
        self.assertEqual((r["sent"], r["left"], r["error"]), (3, 0, None))
        self.assertEqual(ao.failing(self.st.db, t0 + timedelta(hours=1)), [])

    def test_clipped_arguments_are_not_sent_half_parsed(self):
        self.st.log_audit("main", "write_file", {"text": "x" * (aios.AUDIT_MAX + 10)}, "ok", "ok")
        sent = []
        ao.ship_once(self.st.db, "https://c/x", post=lambda u, b, t: sent.append(json.loads(b)))
        self.assertEqual(sent[0]["entries"][0]["args"], "[unparsed: clipped]")


class Collector(http.server.BaseHTTPRequestHandler):
    batches, status, auth = [], 200, []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Collector.auth.append(self.headers.get("Authorization"))
        if Collector.status == 200:
            Collector.batches.append(json.loads(body))
            self.send_response(200)
        else:
            self.send_response(Collector.status)
            self.send_header("Location", "https://elsewhere.example.com/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class RealHttpTests(Case):
    def setUp(self):
        super().setUp()
        Collector.batches, Collector.status, Collector.auth = [], 200, []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Collector)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/aios"

    def test_the_background_sink_sends_entries_from_any_process_with_the_token(self):
        self.log(250)
        with mock.patch.dict("os.environ", {ao.TOKEN_ENV: "tok"}):
            sink = ao.Sink(str(self.home / "aios.db"), self.url, interval=0.05).start()
            other = aios.Store(str(self.home / "aios.db"))  # another AIOS process writing meanwhile
            other.log_audit("serve", "read_file", {"path": "z"}, "t", "ok")
            other.db.close()
            sink.stop()
        ids = [e["id"] for b in Collector.batches for e in b["entries"]]
        self.assertEqual(ids, list(range(1, 252)))
        self.assertEqual(set(Collector.auth), {"Bearer tok"})

    def test_a_redirect_is_a_failure_not_a_new_destination(self):
        self.log(1)
        Collector.status = 307
        r = ao.ship_once(self.st.db, self.url)
        self.assertIsNotNone(r["error"])
        self.assertEqual((r["sent"], r["left"]), (0, 1))


class AnchorTests(Case):
    def test_anchors_only_when_the_chain_grew_and_verifies_against_every_anchor(self):
        self.assertEqual(ao.anchor(self.st.db, self.anchors)["head"], None)  # empty log: nothing
        self.log(3)
        r = ao.anchor(self.st.db, self.anchors)
        self.assertEqual((r["written"], r["id"]), (True, 3))
        self.assertFalse(ao.anchor(self.st.db, self.anchors)["written"])
        self.log(2, 3)
        self.assertEqual(ao.anchor(self.st.db, self.anchors)["id"], 5)
        anchors, problem = ao.read_anchors(self.anchors)
        self.assertEqual(([a["id"] for a in anchors], problem), ([3, 5], None))
        v = ao.verify_anchors(self.st.db, self.anchors)
        self.assertEqual((v["ok"], v["anchors"]), (True, 2))

    def test_a_rebuilt_chain_passes_plain_verify_but_not_the_anchors(self):
        self.log(4)
        ao.anchor(self.st.db, self.anchors)
        self.rebuild_chain(2)
        self.assertTrue(self.st.verify_audit()["ok"])  # the gap anchors close
        v = ao.verify_anchors(self.st.db, self.anchors)
        self.assertFalse(v["ok"])
        self.assertIn("entry 4 no longer has the hash", v["problem"])
        r = ao.anchor(self.st.db, self.anchors)  # and the forgery is never anchored
        self.assertFalse(r["written"])
        self.assertEqual(len(ao.read_anchors(self.anchors)[0]), 1)

    def test_a_log_cut_back_and_reseeded_is_caught(self):
        self.log(4)
        ao.anchor(self.st.db, self.anchors)
        self.st.db.execute("DELETE FROM audit WHERE id>2")
        self.st.db.execute("UPDATE sqlite_sequence SET seq=2 WHERE name='audit'")  # hide the gap
        self.st.db.commit()
        self.assertTrue(self.st.verify_audit()["ok"])
        self.assertIn("entry 4, anchored at", ao.verify_anchors(self.st.db, self.anchors)["problem"])

    def test_a_damaged_anchor_file_is_reported(self):
        self.log(1)
        Path(self.anchors).write_text('{"id": 1, "head": "x"}\nnot json\n', encoding="utf-8")
        self.assertIn("line 2", ao.verify_anchors(self.st.db, self.anchors)["problem"])


class CommandTests(Case):
    def run_cmd(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as e:
            with mock.patch.object(sys, "argv", ["aios", *argv]):
                aios.cli()
        return e.exception.code, out.getvalue()

    def test_audit_anchor_then_verify_anchors(self):
        self.log(2)
        h = str(self.home)
        code, out = self.run_cmd("audit", "--home", h, "--anchor", self.anchors)
        self.assertEqual(code, 0)
        self.assertIn("anchored entry 2", out)
        code, out = self.run_cmd("audit", "--home", h, "--verify", "--anchors", self.anchors)
        self.assertEqual(code, 0)
        self.assertIn("against 1 anchors", out)
        self.rebuild_chain(1)
        code, out = self.run_cmd("audit", "--home", h, "--verify", "--anchors", self.anchors)
        self.assertEqual(code, 1)
        self.assertIn("TAMPERED", out)
        code, out = self.run_cmd("audit", "--home", h, "--anchor", self.anchors)
        self.assertEqual(code, 1)
        self.assertIn("Nothing anchored", out)

    def test_status_shows_how_far_behind_a_sink_is(self):
        self.log(2)
        ao.ship_once(self.st.db, "https://c/x", post=lambda u, b, t: None)
        self.log(1, 2)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            aios.status_cmd(str(self.home))
        self.assertIn("audit sink https://c/x: 1 entries not sent yet", out.getvalue())

    def test_daemon_rejects_plain_http_to_another_machine(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            with mock.patch.object(sys, "argv", ["aios", "daemon", "--home", str(self.home), "--provider", "openai",
                                                 "--model", "m", "--audit-sink", "http://siem.example.com/"]):
                aios.cli()
        self.assertIn("plain http", err.getvalue())


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old = aios.WORKSPACE
        self.addCleanup(setattr, aios, "WORKSPACE", self._old)

    async def test_once_sends_the_backlog_and_anchors_then_releases_the_database(self):
        with tempfile.TemporaryDirectory() as d:
            st = aios.Store(str(Path(d, "aios.db")))
            for i in range(3):
                st.log_audit("main", "read_file", {"path": str(i)}, "t", "ok")
            st.db.close()
            sent, anchors = [], str(Path(d, "anchors.jsonl"))
            with mock.patch.object(ao, "_post", lambda u, b, t: sent.append(json.loads(b))), \
                    contextlib.redirect_stderr(io.StringIO()):
                await aios.daemon(d, once=True, audit_sink="https://c/x", anchor_path=anchors)
            self.assertEqual([e["id"] for b in sent for e in b["entries"]], [1, 2, 3])
            self.assertEqual([a["id"] for a in ao.read_anchors(anchors)[0]], [3])
            Path(d, "aios.db").unlink()  # fails on Windows if the daemon left the file open

    async def test_a_failed_anchor_check_is_an_alert(self):
        with tempfile.TemporaryDirectory() as d:
            st = aios.Store(str(Path(d, "aios.db")))
            st.log_audit("main", "read_file", {"path": "a"}, "t", "ok")
            st.db.close()
            anchors = Path(d, "anchors.jsonl")
            anchors.write_text('{"ts": "x", "id": 1, "head": "0000"}\n', encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                await aios.daemon(d, once=True, anchor_path=str(anchors))
            self.assertIn("ALERT critical audit-tampered", err.getvalue())
            st = aios.Store(str(Path(d, "aios.db")))
            self.assertEqual([a["subject"] for a in wd.list_alerts(st.db)], ["anchors"])
            st.db.close()


if __name__ == "__main__":
    unittest.main()
