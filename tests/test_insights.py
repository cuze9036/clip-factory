"""Weekly self-review: numbers, free-AI advice, the safe changes it makes, and the report. No network.

Run:  python -m unittest tests.test_insights
"""
import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

AI = ({"summary": "Evening Instagram posts did best; morning ones were weak.",
       "working": ["Instagram at 20:00 ET averages the most views"], "not_working": ["12:00 ET posts get few views"],
       "title_tips": ["Start the title with the surprise, in under 6 words"],
       "moment_tips": ["Prefer crowd reactions over talking parts"],
       "suggestions": ["Raise the AI score needed to keep a moment to 8"], "problems": []}, "fake")


class InsightsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _posts(self, n_evening=6, n_noon=6):
        from clipfactory import campaigns, db
        from clipfactory.util import ET, iso
        cid = campaigns.suggest({"name": "Band Live", "platforms": ["instagram"], "rate_per_1k": 2}, "pasted")
        day = (dt.datetime.now(ET) - dt.timedelta(days=3)).date()
        for i in range(n_evening + n_noon):
            evening = i < n_evening
            t = dt.datetime.combine(day, dt.time(20, 3) if evening else dt.time(12, 4), tzinfo=ET)
            m = db.x("INSERT INTO moments(video_id, campaign_id, score, category, hook, created_at) VALUES(?,?,?,?,?,?)",
                     "v1", cid, 9 if evening else 7, "reaction" if evening else "talk", "hook", iso(t))
            k = db.x("INSERT INTO clips(moment_id, campaign_id, title, status, created_at) VALUES(?,?,?,?,?)",
                     m, cid, f"Clip {i}", "posted", iso(t))
            db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, scheduled_at, posted_at, status, views) "
                 "VALUES(?,?,?,?,?,?,?,?)", k, cid, "instagram", "main", iso(t), iso(t), "posted",
                 5000 if evening else 300)
        return cid

    def test_report_tips_and_weak_slot(self):
        from clipfactory import campaigns, insights, llm, schedule
        cid = self._posts()
        with mock.patch.object(llm, "complete_json", return_value=AI) as m:
            report = insights.run(send=False)
        self.assertIn('"instagram 20:00"', m.call_args[0][1])          # slot numbers went to the AI
        self.assertIn("📊 **Weekly review**", report)
        self.assertIn("12 posts made", report)
        self.assertRegex(report, r"✅ Instagram at [56]:30 AM averages the most views")   # 20:00 ET in India time
        self.assertIn("💡 Raise the AI score", report)
        self.assertRegex(report, r"⚙️ Skipping weak posting slots: instagram (9|10):30 PM")
        self.assertEqual(insights.skipped_slots("instagram"), ["12:00"])
        block = llm.rules_block(campaigns.get(cid))
        self.assertIn("Title: Start the title with the surprise", block)
        self.assertIn("Moments: Prefer crowd reactions", block)
        c = campaigns.get(cid)
        with mock.patch("clipfactory.publish.mode", return_value="auto"):
            for _ in range(5):
                when = schedule.next_slot(c, "instagram", "main").astimezone(schedule.ET)
                self.assertNotEqual((when.hour, when.minute // 30), (12, 0))   # 12:00 slot not used

    def test_not_enough_data_changes_nothing(self):
        from clipfactory import db, insights, llm
        self._posts(n_evening=2, n_noon=1)
        with mock.patch.object(llm, "complete_json") as m:
            report = insights.run(send=False)
        m.assert_not_called()
        self.assertIn("Not enough posts yet", report)
        self.assertIsNone(db.kv_get(insights.TIPS_KEY))

    def test_weekly_report_is_sent_in_parts(self):
        from clipfactory import db, insights, llm
        self._posts()
        db.x("INSERT INTO events(at, level, msg) VALUES(?,?,?)", insights.iso(insights.utcnow()), "error",
             "Render failed for clip 4: boom")
        with mock.patch.object(llm, "complete_json", return_value=AI):
            insights.run()
        sent = db.q("SELECT * FROM outbox WHERE kind='insights'")
        self.assertTrue(sent)
        self.assertTrue(all(len(s["text"]) <= 1900 for s in sent))
        self.assertIn("Render failed for clip 4 ×1", "".join(s["text"] for s in sent))

    def test_first_run_only_starts_the_week(self):
        from clipfactory import db, pipeline
        with mock.patch.object(pipeline.watcher, "check_sources"), \
                mock.patch.object(pipeline.campaigns, "active", return_value=[]), \
                mock.patch.object(pipeline.publish, "run_due"), \
                mock.patch("clipfactory.insights.run") as run:
            pipeline.run_once()
        run.assert_not_called()
        self.assertTrue(db.kv_get("insights_last_run"))


if __name__ == "__main__":
    unittest.main()
