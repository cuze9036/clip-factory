"""Paid-promotion tag: first line of every caption for paid campaigns, never on unpaid lanes, and YouTube's
paid-promotion flag. No network.

Run:  python -m unittest tests.test_disclosure
"""
import json
import os
import tempfile
import unittest

PLATFORMS = ["youtube", "instagram", "facebook", "tiktok", "x"]


def _campaign(rate, required=()):
    return {"rate_per_1k": rate, "required_hashtags": list(required), "credit": "Credit: @band"}


def _clip(campaign):
    from clipfactory import metadata
    meta = {"titles": ["HE DID NOT SEE THAT COMING"], "hashtags": ["#music", "#live", "#concert"],
            "description": "The crowd goes wild when the band brings out a surprise guest. " * 3}
    m = metadata.finalize(meta, campaign)
    return {"title": m["titles"][0], "description": m["description"], "hashtags": json.dumps(m["hashtags"])}


class DisclosureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_paid_tag_in_first_100_characters_everywhere_once(self):
        from clipfactory import publish
        camp = _campaign(2.0)
        clip = _clip(camp)
        self.assertNotIn("#Sponsored", clip["description"])        # not stored at the bottom any more
        for p in PLATFORMS:
            cap = publish._caption(clip, p, camp)
            self.assertIn("#Sponsored", cap[:100], p)
            self.assertEqual(cap.count("#Sponsored"), 1, p)
            self.assertEqual(cap.splitlines()[0], "HE DID NOT SEE THAT COMING #Sponsored", p)
        self.assertLessEqual(len(publish._caption(clip, "x", camp)), 280)

    def test_brand_tag_is_used_instead(self):
        from clipfactory import publish
        camp = _campaign(1.5, ["#BandAd", "#music"])
        clip = _clip(camp)
        for p in PLATFORMS:
            cap = publish._caption(clip, p, camp)
            self.assertTrue(cap.splitlines()[0].endswith("#BandAd"), p)
            self.assertEqual(cap.count("#BandAd"), 1, p)
            self.assertNotIn("#Sponsored", cap)

    def test_old_clips_with_tag_at_the_bottom_are_fixed(self):
        from clipfactory import publish
        camp = _campaign(2.0)
        clip = {"title": "WAIT FOR IT", "description": "Big moment.\n\nCredit: @band\n\n#Sponsored #music",
                "hashtags": json.dumps(["#Sponsored", "#music"])}
        cap = publish._caption(clip, "instagram", camp)
        self.assertEqual(cap, "WAIT FOR IT #Sponsored\n\nBig moment.\n\nCredit: @band\n\n#music")

    def test_unpaid_lane_gets_no_tag(self):
        from clipfactory import publish
        camp = _campaign(0)
        clip = _clip(camp)
        for p in PLATFORMS:
            self.assertNotIn("#Sponsored", publish._caption(clip, p, camp), p)
        part, body = publish.youtube_body({"scheduled_at": "2020-01-01T00:00:00Z"}, clip, camp)
        self.assertEqual(part, "snippet,status")
        self.assertNotIn("paidProductPlacementDetails", body)

    def test_youtube_paid_flag_and_description_start(self):
        from clipfactory import publish
        camp = _campaign(2.0)
        clip = _clip(camp)
        part, body = publish.youtube_body({"scheduled_at": "2020-01-01T00:00:00Z"}, clip, camp)
        self.assertEqual(part, "snippet,status,paidProductPlacementDetails")
        self.assertEqual(body["paidProductPlacementDetails"], {"hasPaidProductPlacement": True})
        self.assertTrue(body["snippet"]["description"].startswith("#Sponsored\n\n"))
        self.assertEqual(body["snippet"]["description"].count("#Sponsored"), 1)


if __name__ == "__main__":
    unittest.main()
