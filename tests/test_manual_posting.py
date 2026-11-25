"""Posts you make by hand: video + caption on Telegram early, Discord reminder 30 min before. No network.

Run:  python -m unittest tests.test_manual_posting
"""
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock


class ManualPostingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _post(self, platform, hours_from_now):
        from clipfactory import campaigns, db
        from clipfactory.util import iso, utcnow
        cid = campaigns.suggest({"name": "Band Live", "platforms": [platform], "rate_per_1k": 2}, "pasted")
        video = os.path.join(self.tmp.name, "clip.mp4")
        with open(video, "wb") as f:
            f.write(b"0" * 10)
        clip = db.x("INSERT INTO clips(campaign_id, final_path, title, description, hashtags, status) VALUES(?,?,?,?,?,?)",
                    cid, video, "WAIT FOR IT", "Big moment.", json.dumps(["#music"]), "scheduled")
        when = utcnow() + dt.timedelta(hours=hours_from_now)
        return db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, scheduled_at, status, mode) "
                    "VALUES(?,?,?,?,?,?,?)", clip, cid, platform, "main", iso(when), "queued", "manual")

    def _run(self):
        from clipfactory import publish
        ready = os.path.join(self.tmp.name, "Ready to post")
        with mock.patch("clipfactory.telegram.hand_off") as tg, \
                mock.patch("clipfactory.telegram.creds", return_value=("https://api.telegram.org/botX/", "1")), \
                mock.patch.object(publish, "ready_folder", side_effect=lambda p: __import__("pathlib").Path(
                    os.makedirs(os.path.join(ready, p), exist_ok=True) or os.path.join(ready, p))):
            publish.run_manual()
        return tg

    def test_telegram_early_then_discord_reminder(self):
        from clipfactory import db
        pid = self._post("tiktok", 10)
        tg = self._run()
        tg.assert_called_once()
        when = tg.call_args[0][4]
        self.assertNotIn("ET", when)                             # one time only: the owner's (India)
        self.assertRegex(when, r"\d{1,2}:\d\d [AP]M$")
        self.assertIn("#Sponsored", tg.call_args[0][2].splitlines()[0])
        p = db.one("SELECT * FROM posts WHERE id=?", pid)
        self.assertTrue(p["delivered_at"])
        self.assertEqual(p["status"], "queued")                  # no reminder yet
        self.assertEqual(db.q("SELECT * FROM outbox"), [])
        tg = self._run()                                         # nothing twice
        tg.assert_not_called()
        db.x("UPDATE posts SET scheduled_at=? WHERE id=?",       # now 20 minutes before the time
             (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ"), pid)
        self._run()
        msg = db.q("SELECT * FROM outbox")
        self.assertEqual(len(msg), 1)
        self.assertIn("⏰ **Time to post on TikTok**", msg[0]["text"])
        self.assertIn("within 30 minutes", msg[0]["text"])
        self.assertIn(f"!posted {pid}", msg[0]["text"])
        self.assertIsNone(msg[0]["file_path"])                   # no video on Discord, it's on Telegram
        self.assertEqual(db.one("SELECT status FROM posts WHERE id=?", pid)["status"], "manual")
        self._run()
        self.assertEqual(len(db.q("SELECT * FROM outbox")), 1)   # reminded once

    def test_far_future_post_waits(self):
        self._post("x", 40)
        self._run().assert_not_called()

    def test_waiting_youtube_post_switches_to_automatic(self):
        from clipfactory import db, publish
        pid = self._post("youtube", 5)                            # was planned as a hand-post
        with mock.patch.object(publish, "_has_keys", return_value=True):
            self._run()
        row = db.one("SELECT mode FROM posts WHERE id=?", pid)
        self.assertEqual(row["mode"], "auto")                     # now YouTube uploads it by itself

    def test_announced_hand_post_stays_with_you(self):
        from clipfactory import db, publish
        pid = self._post("youtube", 5)
        db.x("UPDATE posts SET reminded_at='2026-01-01T00:00:00Z' WHERE id=?", pid)
        with mock.patch.object(publish, "_has_keys", return_value=True):
            self._run()
        self.assertEqual(db.one("SELECT mode FROM posts WHERE id=?", pid)["mode"], "manual")

if __name__ == "__main__":
    unittest.main()
