"""Auto-fix: retries (at most twice), Telegram fallback for Instagram/Facebook, extra reminder, AI error notes.

Run:  python -m unittest tests.test_autofix
"""
import datetime as dt
import os
import tempfile
import unittest
from unittest import mock


class AutofixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _camp(self):
        from clipfactory import campaigns
        return campaigns.suggest({"name": "Band Live", "platforms": ["instagram"], "rate_per_1k": 2}, "pasted")

    def test_failed_items_are_retried_twice_at_most(self):
        from clipfactory import autofix, db
        from clipfactory.util import iso, utcnow
        cid = self._camp()
        now = iso(utcnow())
        k = db.x("INSERT INTO clips(campaign_id, status, raw_path, error, created_at) VALUES(?,?,?,?,?)",
                 cid, "failed", "raw.mp4", "ffmpeg crashed", now)
        db.x("INSERT INTO videos(id, campaign_id, title, status, error, created_at) VALUES(?,?,?,?,?,?)",
             "v1", cid, "t", "failed", "not English (es)", now)
        for _ in range(2):
            done = autofix.fix()
            self.assertIn("Retried 1 clip(s) whose editing failed", done)
            self.assertEqual(db.one("SELECT status FROM clips WHERE id=?", k)["status"], "downloaded")
            db.x("UPDATE clips SET status='failed' WHERE id=?", k)   # it fails again
        self.assertEqual(autofix.fix(), [])                          # third time: leave it, the daily check reports it
        self.assertEqual(db.one("SELECT status FROM videos WHERE id='v1'")["status"], "failed")   # not English: never

    def test_instagram_posts_that_cant_go_out_come_to_telegram(self):
        from clipfactory import autofix, db
        from clipfactory.util import iso, utcnow
        cid = self._camp()
        old = iso(utcnow() - dt.timedelta(hours=3))
        k = db.x("INSERT INTO clips(campaign_id, status) VALUES(?,?)", cid, "scheduled")
        p = db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, scheduled_at, status, mode, error) "
                 "VALUES(?,?,?,?,?,?,?,?)", k, cid, "instagram", "main", old, "failed", "auto", "Graph API 400")
        with mock.patch.object(autofix, "_meta_ok", return_value=False):
            done = autofix.fix()
        self.assertIn("key isn't working", done[0])
        row = db.one("SELECT * FROM posts WHERE id=?", p)
        self.assertEqual((row["mode"], row["status"]), ("manual", "queued"))

    def test_missed_hand_post_gets_one_more_reminder(self):
        from clipfactory import autofix, db
        from clipfactory.util import iso, utcnow
        cid = self._camp()
        k = db.x("INSERT INTO clips(campaign_id, status) VALUES(?,?)", cid, "scheduled")
        db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, scheduled_at, status, mode) VALUES(?,?,?,?,?,?,?)",
             k, cid, "tiktok", "main", iso(utcnow() - dt.timedelta(hours=3)), "manual", "manual")
        self.assertEqual(len(autofix.fix()), 1)
        self.assertEqual(autofix.fix(), [])
        self.assertEqual(len(db.q("SELECT * FROM outbox WHERE kind='manual'")), 1)

    def test_errors_explained_once_a_day_with_report_for_claude_code(self):
        from clipfactory import autofix, db, llm
        from clipfactory.util import iso, utcnow
        for _ in range(3):
            db.x("INSERT INTO events(at, level, msg) VALUES(?,?,?)", iso(utcnow()), "error",
                 "Render failed for clip 4: Library cublas64_12.dll is not found")
        ai = ({"cause": "The GPU library for captions is missing.", "owner_fix": "Run the installer again.",
               "needs_code_fix": True}, "fake")
        with mock.patch.object(llm, "complete_json", return_value=ai) as m:
            done, notes = autofix.run()
            autofix.run()
        self.assertEqual(m.call_count, 1)
        self.assertIn("🔍 **Render failed for clip 4** ×3", notes[0])
        self.assertIn("Send this to Claude Code", notes[0])
        self.assertIn("cublas64_12.dll", notes[0])
        sent = db.q("SELECT kind FROM outbox")
        self.assertEqual([s["kind"] for s in sent], ["health"])

    def test_daily_check_lists_fixes(self):
        from clipfactory import autofix, db, health
        from clipfactory.util import iso, utcnow
        cid = self._camp()
        db.x("INSERT INTO clips(campaign_id, status, raw_path, error, created_at) VALUES(?,?,?,?,?)",
             cid, "failed", "raw.mp4", "boom", iso(utcnow()))
        autofix.run()
        self.assertIn("🛠️ **Fixed by itself (24 h)**\n• Retried 1 clip(s)", health.report())


if __name__ == "__main__":
    unittest.main()
