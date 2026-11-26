"""Daily study (lessons from our own views), TikTok/X screenshots, and submit messages that expire. No network.

Run:  python -m unittest tests.test_study
"""
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

AI = {"summary": "Clips with a fast start got the most views.",
      "do": [{"lesson": "Start on the loudest moment", "evidence": "post 1: 9,000 views", "confidence": "medium"}],
      "avoid": [{"lesson": "Long intros", "evidence": "post 2: 40 views", "confidence": "high"},
                {"lesson": "Maybe nothing", "evidence": "", "confidence": "bogus"}],
      "best_clip": "Big win", "worst_clip": "Slow one"}


class StudyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _posts(self, views=(9000, 40, 700), platform="instagram"):
        from clipfactory import campaigns, db
        from clipfactory.util import iso, utcnow
        cid = campaigns.suggest({"name": "Band Live", "platforms": [platform], "rate_per_1k": 2}, "pasted")
        ids = []
        for i, v in enumerate(views):
            k = db.x("INSERT INTO clips(campaign_id, status, title, description, style, duration_s) VALUES(?,?,?,?,?,?)",
                     cid, "posted", f"Clip number {i} title", f"Caption of clip {i}", "split", 30)
            ids.append(db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, posted_at, scheduled_at, status, "
                            "views, external_id) VALUES(?,?,?,?,?,?,?,?,?)", k, cid, platform, "main",
                            iso(utcnow() - dt.timedelta(days=1 + i)), iso(utcnow() - dt.timedelta(days=1 + i)),
                            "posted", v, f"m{i}"))
        return cid, ids

    def test_daily_study_writes_lessons_and_feeds_prompts(self):
        from clipfactory import analytics, campaigns, llm, study
        cid, _ = self._posts()
        with mock.patch.object(analytics, "refresh"), mock.patch.object(study, "meta_details", return_value={"reach": 5}), \
                mock.patch.object(llm, "complete_json", side_effect=lambda t, p, v, **k: (v(AI), "gemini")) as ai:
            msg = study.run(send=False)
        self.assertIn("Daily study", msg)
        self.assertIn('"reach": 5', ai.call_args[0][1])                       # Instagram details went to the AI
        d = study.folder()
        self.assertTrue((d / "lessons.md").exists())
        self.assertEqual(len(list((d / "snapshots").glob("*.json"))), 1)
        saved = json.loads((d / "lessons.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["avoid"][1]["confidence"], "low")              # unknown confidence → low
        block = llm.rules_block(campaigns.get(cid))
        self.assertIn("Do: Start on the loudest moment", block)
        self.assertIn("Avoid: Long intros", block)
        self.assertNotIn("Maybe nothing", block)                              # low "avoid" lines stay out

    def test_too_few_clips_only_saves_numbers(self):
        from clipfactory import analytics, llm, study
        self._posts(views=(5,))
        with mock.patch.object(analytics, "refresh"), mock.patch.object(study, "meta_details", return_value={}), \
                mock.patch.object(llm, "complete_json") as ai:
            msg = study.run(send=False)
        ai.assert_not_called()
        self.assertIn("Not enough", msg)

    def test_old_youtube_snapshots_are_deleted(self):
        from clipfactory import study
        old = study.folder() / "snapshots" / "2000-01-01.json"
        old.write_text("{}")
        self.assertEqual(study.prune(), 1)
        self.assertFalse(old.exists())

    def test_screenshot_numbers_land_on_the_right_clip(self):
        from clipfactory import db, llm, study
        _, ids = self._posts(views=(0, 0), platform="tiktok")
        shot = os.path.join(self.tmp.name, "s.png")
        with open(shot, "wb") as f:
            f.write(b"png")
        read = {"platform": "other", "kind": "post list", "posts": [
            {"text": "Clip number 1 title", "views": "12300", "likes": 800},
            {"text": "Someone else's video", "views": 5}], "account": {}, "notes": ""}
        with mock.patch.object(llm, "complete_json", side_effect=lambda t, p, v, video: (v(read), "gemini")) as ai:
            reply = study.read_screenshot(shot, "tiktok")
        self.assertEqual(ai.call_args.kwargs["video"]["image"], shot)
        self.assertIn("post 2: 12,300 views", reply)
        self.assertIn("1 not matched", reply)
        self.assertEqual(db.one("SELECT views FROM posts WHERE id=?", ids[1])["views"], 12300)
        self.assertEqual(study.screenshot_notes()[0]["platform"], "tiktok")


class SubmitExpiryTest(unittest.TestCase):
    setUp, tearDown, _posts = StudyTest.setUp, StudyTest.tearDown, StudyTest._posts

    def test_unsubmitted_messages_leave_after_30_minutes(self):
        from clipfactory import db, notify, publish
        from clipfactory.util import iso, utcnow
        _, ids = self._posts(views=(0, 0))
        old = iso(utcnow() - dt.timedelta(minutes=35))
        for pid in ids:
            notify.send("submit", "📌 Submit now", post_id=pid)
            notify.send("submit", "https://link", post_id=pid)
        db.x("UPDATE outbox SET created_at=?, sent_at=?, message_id=id+100 WHERE post_id=?", old, old, ids[0])
        db.x("UPDATE posts SET submitted=1 WHERE id=?", ids[1])   # done: handled by the ✅ Done button
        self.assertEqual(publish.expire_submits(), 1)
        self.assertEqual(sorted(str(i) for _, i in db.kv_get("discord_delete")),
                         sorted(str(r["id"] + 100) for r in db.q("SELECT id FROM outbox WHERE post_id=?", ids[0])))
        self.assertEqual(publish.expire_submits(), 0)                       # once
        fresh = db.q("SELECT * FROM outbox WHERE post_id=? AND sent_at IS NULL", ids[1])
        self.assertEqual(len(fresh), 2)                                     # the other post's messages untouched


if __name__ == "__main__":
    unittest.main()
