"""Timing: posts land at random minutes around their slots; new sources are checked right away. No network.

Run:  python -m unittest tests.test_timing
"""
import datetime as dt
import os
import tempfile
import unittest
from unittest import mock


class TimingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_slots_vary_within_jitter(self):
        from clipfactory import campaigns, schedule
        from clipfactory.util import ET
        cid = campaigns.suggest({"name": "Band Live", "platforms": ["youtube"], "rate_per_1k": 2}, "pasted")
        c = campaigns.get(cid)
        start = dt.datetime(2030, 1, 7, 6, 0, tzinfo=ET).astimezone(dt.timezone.utc)
        seen = set()
        with mock.patch.object(schedule, "utcnow", return_value=start):
            for _ in range(20):
                when = schedule.next_slot(c, "instagram", "main").astimezone(ET)
                base = min((dt.datetime.combine(when.date(), dt.time(*map(int, hm.split(":"))), tzinfo=ET)
                            for hm in ("12:00", "15:00", "18:30", "20:00", "21:30")), key=lambda b: abs(when - b))
                self.assertLessEqual(abs((when - base).total_seconds()), 12 * 60)
                seen.add((when.minute, when.second))
        self.assertGreater(len(seen), 5)                  # not the same minute every time

    def test_manual_platforms_use_owner_friendly_slots(self):
        from clipfactory import campaigns, schedule
        from clipfactory.util import ET
        cid = campaigns.suggest({"name": "Band Live", "platforms": ["tiktok"], "rate_per_1k": 2}, "pasted")
        c = campaigns.get(cid)
        start = dt.datetime(2030, 1, 7, 6, 0, tzinfo=ET).astimezone(dt.timezone.utc)
        with mock.patch.object(schedule, "utcnow", return_value=start), \
                mock.patch("clipfactory.publish.mode", side_effect=lambda p, a: "manual"):
            when = schedule.next_slot(c, "tiktok", "main").astimezone(ET)
        self.assertEqual((when.date(), when.hour), (dt.date(2030, 1, 7), 12))   # 12:30 ET ± jitter
        with mock.patch.object(schedule, "utcnow", return_value=start), \
                mock.patch("clipfactory.publish.mode", side_effect=lambda p, a: "auto"):
            when = schedule.next_slot(c, "x", "main").astimezone(ET)
        self.assertIn(when.hour, (8,))                                            # 08:30 ET ± jitter

    def test_new_source_is_checked_right_away(self):
        from clipfactory import campaigns, db, pipeline
        cid = campaigns.suggest({"name": "Band Live", "platforms": ["youtube"], "rate_per_1k": 2}, "pasted")
        db.kv_set("watcher_last_run", campaigns.iso(campaigns.utcnow()))   # just checked
        db.kv_set("watcher_now", None)
        campaigns.add_source(cid, "https://www.dropbox.com/scl/fo/a/b?rlkey=k&dl=0")
        self.assertTrue(db.kv_get("watcher_now"))
        with mock.patch.object(pipeline.watcher, "check_sources") as chk, \
                mock.patch.object(pipeline.campaigns, "active", return_value=[]), \
                mock.patch.object(pipeline.publish, "run_due"), \
                mock.patch.object(pipeline, "_due", return_value=False):
            pipeline.run_once()
        chk.assert_called_once()
        self.assertFalse(db.kv_get("watcher_now"))


    def test_work_cut_off_by_a_restart_resumes(self):
        from clipfactory import campaigns, db, pipeline
        cid = campaigns.suggest({"name": "Band Live", "platforms": ["youtube"], "rate_per_1k": 2}, "pasted")
        db.x("INSERT INTO videos(id, campaign_id, title, status) VALUES(?,?,?,?)", "v1", cid, "a", "analyzing")
        db.x("INSERT INTO moments(video_id, campaign_id, status) VALUES(?,?,?)", "v1", cid, "downloading")
        pipeline.resume_interrupted()
        self.assertEqual(db.one("SELECT status FROM videos WHERE id='v1'")["status"], "new")
        self.assertEqual(db.one("SELECT status FROM moments")["status"], "found")


if __name__ == "__main__":
    unittest.main()
