"""Live campaigns, "I joined" + pasted page, #campaign-feed and the finder's live tracking. No network.

Run:  python -m unittest tests.test_joinflow
"""
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock


def _listing(title, budget=12000, rate=2.0, clippers=9):
    return {"title": title, "rewardPerThousandUsd": rate, "budgetLeftUsd": budget, "creatorsCount": clippers,
            "socialPlatforms": ["youtube", "instagram", "tiktok"], "payoutType": "cpm"}


class JoinFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _found(self, title, **kw):
        from clipfactory import discovery
        item = _listing(title, **kw)
        cid, _ = discovery.add_from_data(discovery.from_listing(item), json.dumps(item), "listing fields",
                                         "name:" + title.lower().replace(" ", ""), notify_always=False)
        return cid

    def test_live_list_has_open_and_join_buttons(self):
        from clipfactory import db, joinflow
        a = self._found("Band Live Clipping", rate=2.0)
        b = self._found("Cheap Clipping", rate=0.6)
        old = self._found("Old Clipping")
        db.x("UPDATE campaigns SET seen_at=? WHERE id=?", "2020-01-01T00:00:00Z", old)
        db.x("DELETE FROM outbox")
        reply = joinflow.post_live()
        self.assertIn("2 live campaign(s)", reply)
        cards = db.q("SELECT * FROM outbox ORDER BY id")
        self.assertEqual([c["campaign_id"] for c in cards], [a, b])          # best pay first, old one gone
        btn = json.loads(cards[0]["buttons"])
        self.assertEqual(btn[0][2], "link")
        self.assertTrue(btn[0][1].startswith("https://www.google.com/search?q="))  # no link on free Apify: search
        self.assertEqual(btn[1], ["I joined", f"join:{a}", "green"])

    def test_joined_then_pasted_page_starts_the_lane(self):
        from clipfactory import campaigns, commands, joinflow
        cid = self._found("Band Live Clipping")
        reply = commands.button(f"join:{cid}")
        self.assertIn("Ctrl+A", reply)
        self.assertEqual(joinflow.awaiting(), cid)
        page = {"name": "Band Live", "rate_per_1k": 2, "budget_left": 11800, "required_hashtags": ["#BandAd"],
                "credit": "@band", "rules": ["Show the band name"], "clip_length": [10, 40],
                "sources": ["https://www.youtube.com/@BandOfficial"], "platforms": ["youtube", "tiktok"]}
        with mock.patch.object(campaigns, "extract_from_brief", return_value=(page, "fake")):
            out = commands.brief(cid, "x" * 200)
        c = campaigns.get(cid)
        self.assertIn("The lane is running", out)
        self.assertEqual(c["stage"], "active")
        self.assertEqual(c["required_hashtags"], ["#BandAd"])
        self.assertEqual((c["min_len"], c["max_len"]), (10, 40))
        self.assertEqual(campaigns.sources(cid)[0]["channel_id"], "@BandOfficial")
        self.assertIsNone(joinflow.awaiting())

    def test_drive_footage_asks_for_a_synced_folder(self):
        from clipfactory import campaigns, joinflow
        cid = self._found("Drive Clipping")
        joinflow.joined(cid)
        page = {"name": "Drive", "sources": ["https://drive.google.com/drive/folders/abc123"]}
        with mock.patch.object(campaigns, "extract_from_brief", return_value=(page, "fake")):
            out = joinflow.apply_brief(cid, "Footage: https://drive.google.com/drive/folders/abc123 " + "x" * 100)
        self.assertIn("Google Drive", out)
        self.assertIn("drive.google.com/drive/folders/abc123", out)
        self.assertEqual(campaigns.get(cid)["stage"], "suggested")   # not started without footage

    def test_campaign_paste_goes_to_the_joined_campaign(self):
        from clipfactory import campaigns, commands, joinflow
        cid = self._found("Band Live Clipping")
        joinflow.joined(cid)
        with mock.patch.object(campaigns, "extract_from_brief",
                               return_value=({"name": "x", "sources": ["@BandOfficial"]}, "fake")):
            out = commands.handle("!campaign " + "page text " * 10)
        self.assertIn("Band Live Clipping", out)
        self.assertEqual(len([c for c in campaigns.all_campaigns()]), 1)   # no duplicate created

    def test_feed_post_becomes_a_live_suggestion_once(self):
        from clipfactory import campaigns, db, joinflow
        db.x("DELETE FROM outbox")
        self.assertIsNone(joinflow.from_feed("gm everyone, server rules updated", 1))
        post = ("NEW CAMPAIGN: Big Streamer clips - $1.50 per 1K views, $20,000 budget. "
                "Join here https://whop.com/big-streamer/clips/")
        data = {"name": "Big Streamer clips", "rate_per_1k": 1.5, "budget_left": 20000, "platforms": ["youtube"]}
        with mock.patch.object(campaigns, "extract_from_brief", return_value=(dict(data), "fake")) as m:
            cid = joinflow.from_feed(post, 2)
            again = joinflow.from_feed(post, 2)
        self.assertEqual(m.call_count, 1)
        self.assertIsNone(again)
        c = campaigns.get(cid)
        self.assertEqual(c["origin"], "feed")
        self.assertEqual(c["url"], "https://whop.com/big-streamer/clips/")
        self.assertTrue(joinflow.is_live(c))
        btn = json.loads(db.q("SELECT buttons FROM outbox")[0]["buttons"])
        self.assertEqual(btn[0], ["Open campaign", "https://whop.com/big-streamer/clips/", "link"])

    def test_finder_keeps_listed_campaigns_live_and_updates_budget(self):
        from clipfactory import campaigns, db, discovery
        cid = self._found("Band Live Clipping", budget=12000)
        db.x("UPDATE campaigns SET seen_at=? WHERE id=?", "2020-01-01T00:00:00Z", cid)
        with mock.patch.object(discovery, "fetch_listings",
                               return_value=[_listing("Band Live Clipping", budget=9000, clippers=15)]):
            discovery.run()
        c = campaigns.get(cid)
        self.assertEqual(c["budget_left"], 9000)
        self.assertEqual(campaigns._extra(c)["clippers"], 15)
        self.assertGreater(c["seen_at"], (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M"))


if __name__ == "__main__":
    unittest.main()
