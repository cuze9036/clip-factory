"""Bug reports to Claude Code as GitHub issues: scrubbed, one issue per error, new one after a fix. No network.

Run:  python -m unittest tests.test_bugreport
"""
import os
import tempfile
import unittest
from unittest import mock


class BugReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ.update(CLIPFACTORY_DATA=self.tmp.name, GITHUB_ISSUES_TOKEN="github_pat_x")
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        os.environ.pop("GITHUB_ISSUES_TOKEN", None)
        self.tmp.cleanup()

    def test_scrub_removes_secrets_and_names(self):
        from clipfactory import bugreport
        text = (r"C:\Users\KHUSHAI\Desktop\Clipping Tools\x.py failed for me@gmail.com token EAAabcdefghijklmnopqrstuvwxyz12 "
                "https://www.dropbox.com/scl/fo/a/b?rlkey=SECRET123&dl=1 bot 9098321765:AAG1wabcdefghijklmnopqrstuvwxyz1234 "
                "https://hc-ping.com/1234-abcd")
        out = bugreport.scrub(text)
        for bad in ("KHUSHAI", "me@gmail.com", "EAAabc", "SECRET123", "AAG1w", "1234-abcd"):
            self.assertNotIn(bad, out)
        self.assertIn(r"C:\Users\<user>", out)

    def test_one_issue_per_error_then_a_new_one_after_a_fix(self):
        from clipfactory import bugreport
        calls = []

        def fake(method, url, **kw):
            calls.append((method, url.split("/repos/")[1], kw.get("json")))
            if method == "GET":
                return mock.Mock(status_code=200, json=lambda: {"state": state["v"]})
            return mock.Mock(status_code=201, json=lambda: {"number": 7 if len(calls) < 3 else 8})

        state = {"v": "open"}
        with mock.patch("requests.request", fake):
            self.assertEqual(bugreport.report("Render failed for clip 4", 3, r"boom at C:\Users\KHUSHAI\x"), 7)
            self.assertEqual(bugreport.report("Render failed for clip 4", 5, "boom"), 7)   # same day: nothing new
            state["v"] = "closed"
            self.assertEqual(bugreport.report("Render failed for clip 4", 2, "boom again"), 8)
        posts = [c for c in calls if c[0] == "POST"]
        self.assertEqual(posts[0][1], "cuze9036/clip-factory/issues")
        self.assertTrue(posts[0][2]["title"].startswith("[auto-report] Render failed"))
        self.assertNotIn("KHUSHAI", posts[0][2]["body"])
        self.assertEqual(len(posts), 2)

    def test_autofix_reports_code_bugs(self):
        from clipfactory import autofix, db, llm
        from clipfactory.util import iso, utcnow
        for _ in range(3):
            db.x("INSERT INTO events(at, level, msg) VALUES(?,?,?)", iso(utcnow()), "error", "Render failed for clip 4: KeyError 'x'")
        ai = ({"cause": "A missing field in the code.", "owner_fix": "", "needs_code_fix": True}, "fake")
        with mock.patch.object(llm, "complete_json", return_value=ai), \
                mock.patch("clipfactory.bugreport.report", return_value=12) as rep:
            _, notes = autofix.run()
        rep.assert_called_once()
        self.assertIn("reported to Claude Code (GitHub issue #12)", notes[0])

    def test_not_set_up_means_skip(self):
        from clipfactory import selftest
        os.environ.pop("GITHUB_ISSUES_TOKEN")
        self.assertTrue(selftest._check("Bug reports", selftest._bugs).startswith("SKIP"))


if __name__ == "__main__":
    unittest.main()
