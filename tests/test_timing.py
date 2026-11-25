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
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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

    def test_campaign_clips_go_live_only_while_owner_is_awake(self):
        from zoneinfo import ZoneInfo
        from clipfactory import campaigns, schedule
        from clipfactory.util import ET
        ist = ZoneInfo("Asia/Kolkata")
        cid = campaigns.suggest({"name": "Band Live", "platforms": ["youtube"], "rate_per_1k": 2}, "pasted")
        c = campaigns.get(cid)
        # 12:30 ET = 23:00 IST: the 15:30/18:00/20:00 ET YouTube slots are 02:00-06:30 IST, so the next one is 22:00 ET
        start = dt.datetime(2030, 1, 7, 12, 30, tzinfo=ET).astimezone(dt.timezone.utc)
        with mock.patch.object(schedule, "utcnow", return_value=start),                 mock.patch("clipfactory.publish.mode", side_effect=lambda p, a: "auto"):
            for _ in range(10):
                when = schedule.next_slot(c, "youtube", "main")
                t = when.astimezone(ist).time()
                self.assertTrue(dt.time(8, 0) <= t or t <= dt.time(0, 0), t)
            self.assertEqual(when.astimezone(ET).hour in (21, 22), True)
            own = dict(c, origin="yaml", url="")          # own channel: nothing to submit, US prime time as before
            self.assertEqual(schedule.next_slot(own, "youtube", "main").astimezone(ET).hour in (15, 16), True)

    def test_late_campaign_post_waits_for_morning(self):
        from clipfactory import campaigns, db, publish, schedule
        from clipfactory.util import ET, iso
        cid = campaigns.suggest({"name": "Band Live", "platforms": ["instagram"], "rate_per_1k": 2}, "pasted")
        db.x("INSERT INTO clips(campaign_id, status) VALUES(?, 'scheduled')", cid)
        night = dt.datetime(2030, 1, 7, 17, 0, tzinfo=ET).astimezone(dt.timezone.utc)   # 03:30 IST
        db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, scheduled_at, mode) VALUES(1,?,?,?,?,?)",
             cid, "instagram", "main", iso(night), "auto")
        up = mock.Mock(return_value=("1", "https://x/1"))
        with mock.patch.object(publish, "utcnow", return_value=night),                 mock.patch.object(schedule, "utcnow", return_value=night),                 mock.patch("clipfactory.publish.mode", side_effect=lambda p, a: "auto"),                 mock.patch.dict(publish.UPLOADERS, {"instagram": up}),                 mock.patch.object(publish, "run_manual"), mock.patch.object(publish, "submit_nudges"):
            publish.run_due()
        up.assert_not_called()
        p = db.one("SELECT * FROM posts WHERE id=1")
        self.assertEqual(p["status"], "queued")
        self.assertTrue(schedule.awake(publish.parse_iso(p["scheduled_at"]), 30))

    def test_owner_sees_india_time_only(self):
        from clipfactory.util import ET, own_time, own_times_in
        now = dt.datetime(2030, 1, 7, 12, 0, tzinfo=ET)                     # 10:30 PM in India
        self.assertEqual(own_time(now, now), "today 10:30 PM")
        self.assertEqual(own_time(now + dt.timedelta(hours=8), now), "tomorrow 6:30 AM")
        self.assertEqual(own_time(now + dt.timedelta(days=3), now), "Thu 10 Jan, 10:30 PM")
        self.assertRegex(own_times_in("Posts at 20:00 ET do best"), r"Posts at [56]:30 AM do best")

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
