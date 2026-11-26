"""Faster clips until a campaign earns $50, the campaign's comment under every post, and helper posts that expire.
No network.

Run:  python -m unittest tests.test_boost
"""
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock


class BoostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _campaign(self, views=0):
        from clipfactory import campaigns, db
        cid = campaigns.suggest({"name": "Lucia", "platforms": ["instagram", "tiktok"], "rate_per_1k": 1.5}, "pasted")
        db.x("UPDATE campaigns SET stage='active', paused=0, daily_clip_quota=2 WHERE id=?", cid)
        if views:
            k = db.x("INSERT INTO clips(campaign_id, status) VALUES(?, 'posted')", cid)
            db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, status, views, posted_at) "
                 "VALUES(?,?,?,?,?,?,?)", k, cid, "instagram", "main", "posted", views, "2026-01-01T00:00:00Z")
        return cid

    def test_quota_is_boosted_until_the_campaign_earned_50(self):
        from clipfactory import campaigns, flops
        poor, rich = self._campaign(views=1000), self._campaign(views=40000)   # $1.50 vs $60
        self.assertTrue(campaigns.boosting(campaigns.get(poor)))
        self.assertGreaterEqual(flops.quota(campaigns.get(poor)), 6)
        self.assertFalse(campaigns.boosting(campaigns.get(rich)))
        self.assertLess(flops.quota(campaigns.get(rich)), 6)

    def test_campaign_comment_goes_under_instagram_posts_and_to_the_owner(self):
        from clipfactory import campaigns, db, publish
        cid = self._campaign()
        extra = campaigns._extra(campaigns.get(cid))
        extra["comment_cta"] = ["she's AI btw. you can make your own on creatorinseconds.com"]
        db.x("UPDATE campaigns SET notes=?, url='https://contentrewards.com/c/campaigns/x' WHERE id=?",
             json.dumps(extra), cid)
        k = db.x("INSERT INTO clips(campaign_id, status) VALUES(?, 'posted')", cid)
        pid = db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, status, url, posted_at) "
                   "VALUES(?,?,?,?,?,?,?)", k, cid, "instagram", "main", "posted", "https://instagram.com/reel/x",
                   "2026-01-01T00:00:00Z")
        post = db.one("SELECT * FROM posts WHERE id=?", pid)
        with mock.patch.object(publish, "meta_keys", return_value=("1", "tok")), \
                mock.patch.object(publish, "_g", return_value={"id": "c1"}) as g:
            self.assertTrue(publish.comment_cta(post, "media1"))
        self.assertEqual(g.call_args.kwargs["data"]["message"], extra["comment_cta"][0])
        publish.submit_reminder(pid)
        head = db.one("SELECT text FROM outbox WHERE kind='submit' ORDER BY id LIMIT 1")["text"]
        self.assertIn("pin it", head)

    def test_helper_posts_leave_the_channel_24h_after_their_time(self):
        from clipfactory import db, notify, poster
        from clipfactory.util import iso, utcnow
        cid = self._campaign()
        k = db.x("INSERT INTO clips(campaign_id, status) VALUES(?, 'scheduled')", cid)
        old = db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, status, mode, scheduled_at) "
                   "VALUES(?,?,?,?,?,?,?)", k, cid, "tiktok", "main", "manual", "manual",
                   iso(utcnow() - dt.timedelta(hours=25)))
        new = db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, status, mode, scheduled_at) "
                   "VALUES(?,?,?,?,?,?,?)", k, cid, "x", "main", "manual", "manual", iso(utcnow() - dt.timedelta(hours=2)))
        for pid in (old, new):
            notify.mark_sent(notify.send("poster", "video", post_id=pid), str(500 + pid))
        self.assertEqual(poster.expire(), 1)
        self.assertEqual(db.kv_get("discord_delete"), [["poster", str(500 + old)]])
        self.assertEqual(db.one("SELECT status FROM posts WHERE id=?", old)["status"], "failed")
        self.assertEqual(db.one("SELECT status FROM posts WHERE id=?", new)["status"], "manual")
        self.assertEqual(poster.expire(), 0)

    def test_short_clips_are_footage_for_a_campaign_format(self):
        from clipfactory import campaigns, db, recipes, watcher
        cid = self._campaign()
        db.x("INSERT INTO videos(id, campaign_id, title, status, error) VALUES('v1', ?, 'v1', 'skipped', 'too short')", cid)
        recipes.set_recipe(cid, os.path.join(self.tmp.name, "card.mp4"))
        self.assertEqual(db.one("SELECT status FROM videos WHERE id='v1'")["status"], "waiting")
        seen = {}
        with mock.patch.object(campaigns, "sources", return_value=[{"id": 1, "kind": "folder", "name": "f",
                                                                      "channel_id": self.tmp.name}]), \
                mock.patch.object(watcher, "_folder", side_effect=lambda c, s, m: seen.setdefault(c["id"], m) and 0):
            watcher.check_sources()
        self.assertEqual(seen[cid], 3)


if __name__ == "__main__":
    unittest.main()
