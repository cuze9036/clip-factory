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
                base = dt.datetime.combine(when.date(), dt.time(9, 0), tzinfo=ET) + dt.timedelta(minutes=90)
                self.assertLessEqual(abs((when - base).total_seconds()), 12 * 60)
                seen.add((when.minute, when.second))
        self.assertGreater(len(seen), 5)                  # not the same minute every time

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


if __name__ == "__main__":
    unittest.main()
