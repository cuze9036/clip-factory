"""TikTok and X: recognised in campaign pages, handed to you on Discord/Telegram, short captions for X.

Run:  python -m unittest tests.test_platforms
"""
import json
import os
import tempfile
import unittest


class PlatformsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_campaign_page_platforms(self):
        from clipfactory import campaigns
        d = campaigns._extract_validate({"name": "A", "platforms": ["TikTok", "Twitter", "Instagram Reels", "Snapchat"],
                                         "topic": "music", "rules": []})
        self.assertEqual(d["platforms"], ["tiktok", "x", "instagram"])
        self.assertEqual(d["unsupported_platforms"], ["snapchat"])

    def test_listing_platforms(self):
        from clipfactory import discovery
        data = discovery.from_listing({"title": "B", "rewardPerThousandUsd": 1, "budgetLeftUsd": 900,
                                       "socialPlatforms": ["x", "tiktok", "facebook"]})
        self.assertEqual(data["platforms"], ["x", "tiktok", "facebook"])

    def test_tiktok_and_x_are_handed_to_you(self):
        from clipfactory import publish
        self.assertEqual(publish.mode("tiktok", "main"), "manual")
        self.assertEqual(publish.mode("x", "main"), "manual")
        self.assertEqual(publish.name("tiktok"), "TikTok")

    def test_x_caption_fits_280(self):
        from clipfactory import publish
        clip = {"title": "HE DID NOT SEE THAT COMING", "hashtags": json.dumps(["#Sponsored", "#music", "#live"]),
                "description": ("A very long description. " * 30) + "\n\nCredit: @band\n\n#Sponsored #music #live"}
        x = publish._caption(clip, "x")
        self.assertLessEqual(len(x), 280)
        self.assertIn("Credit: @band", x)
        self.assertIn("#Sponsored", x)
        self.assertGreater(len(publish._caption(clip, "tiktok")), 280)


if __name__ == "__main__":
    unittest.main()
