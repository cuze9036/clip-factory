"""Campaign check: the whole campaign is read before any video is watched; 'no' pauses, Resume overrides.

Run:  python -m unittest tests.test_check
"""
import os
import tempfile
import unittest
from unittest import mock

BRIEF = ("Call of Duty clipping campaign. Post gameplay highlights from the provided Drive. Your account name and "
         "profile picture must not be related to Call of Duty. Use an existing account or create a new public one.")
GO = ({"verdict": "go", "summary": "Clips of CoD highlights from the Drive footage.", "can": ["Cut highlights"],
       "cannot": [], "account_rules": ["Account name and profile picture must not be related to Call of Duty"],
       "owner_todo": ["Check your account name and picture are not about Call of Duty"]}, "fake")
NO = ({"verdict": "no", "summary": "Needs face-cam reaction videos filmed by you.", "can": [],
       "cannot": ["Film your own reactions"], "account_rules": [], "owner_todo": []}, "fake")


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import campaigns, db
        db._local.__dict__.clear()
        campaigns.save("cod", {"name": "Call of Duty Clips", "platforms": ["youtube"], "brief": BRIEF,
                               "rules": ["No reuploads"]}, "pasted")
        db.x("UPDATE campaigns SET stage='active' WHERE id='cod'")

    def tearDown(self):
        self.tmp.cleanup()

    def c(self):
        from clipfactory import campaigns
        return campaigns.get("cod")

    def test_go_is_checked_once_and_reported(self):
        from clipfactory import check, llm, notify
        with mock.patch.object(llm, "complete_json", return_value=GO) as ai, \
                mock.patch.object(notify, "send") as sent:
            self.assertTrue(check.ready(self.c()))
            self.assertTrue(check.ready(self.c()))
        self.assertEqual(ai.call_count, 1)
        prompt = ai.call_args.args[1]
        self.assertIn("profile picture must not be related", prompt)
        text = sent.call_args.args[1]
        self.assertIn("We can do this", text)
        self.assertIn("Account rules", text)
        self.assertIn("not about Call of Duty", text)
        self.assertIn("Check: ✅", check.card_line(self.c()))

    def test_changed_rules_check_again(self):
        from clipfactory import check, db, llm, notify
        with mock.patch.object(llm, "complete_json", return_value=GO) as ai, mock.patch.object(notify, "send"):
            check.ready(self.c())
            db.x("""UPDATE campaigns SET rules='["No reuploads", "Only English"]' WHERE id='cod'""")
            check.ready(self.c())
        self.assertEqual(ai.call_count, 2)

    def test_no_pauses_and_resume_overrides(self):
        from clipfactory import campaigns, check, llm, notify
        with mock.patch.object(llm, "complete_json", return_value=NO), mock.patch.object(notify, "send") as sent:
            self.assertFalse(check.ready(self.c()))
        self.assertTrue(self.c()["paused"])
        self.assertIn("lane is paused", sent.call_args.args[1])
        campaigns.set_paused("cod", False)
        with mock.patch.object(llm, "complete_json") as ai:
            self.assertTrue(check.ready(self.c()))
        ai.assert_not_called()

    def test_analysis_waits_for_the_check(self):
        from clipfactory import check, db, pipeline
        db.x("""INSERT INTO videos(id, campaign_id, title, status, created_at) VALUES('v1','cod','A','new','x')""")
        with mock.patch.object(check, "ready", return_value=False), \
                mock.patch("clipfactory.analyst.analyze") as an:
            pipeline.stage_analyze(self.c())
        an.assert_not_called()
        self.assertEqual(db.one("SELECT status FROM videos WHERE id='v1'")["status"], "new")

    def test_ai_down_does_not_block_forever(self):
        from clipfactory import check, llm
        with mock.patch.object(llm, "complete_json", side_effect=llm.LLMError("down")):
            self.assertEqual([check.ready(self.c()) for _ in range(3)], [False, False, True])

    def test_a_blocker_makes_it_no_whatever_the_verdict(self):
        from clipfactory import check
        base = {"verdict": "go", "summary": "Swap given shots in streamer videos.", "can": ["Cut clips"],
                "cannot": [], "account_rules": [], "owner_todo": []}
        d = check._validate(dict(base, needs={"shot_by_shot_edit": True, "non_english_text": False}))
        self.assertEqual(d["verdict"], "no")
        self.assertIn("shot-by-shot", d["cannot"][0])
        self.assertEqual(check._validate(dict(base, needs={"shot_by_shot_edit": False}))["verdict"], "go")
        self.assertEqual(check._validate(base)["verdict"], "go")   # older answers without "needs" still work

    def test_no_added_text_meets_a_language_rule(self):
        import json
        from unittest import mock
        from clipfactory import campaigns, check, db, llm
        cid = campaigns.suggest({"name": "CoD Arabic", "platforms": ["instagram"], "rate_per_1k": 1}, "pasted")
        db.x("UPDATE campaigns SET brief='Captions must be in Arabic' WHERE id=?", cid)
        ans = {"verdict": "go", "summary": "Arabic clips.", "can": ["cut"], "cannot": [], "account_rules": [],
               "owner_todo": [], "needs": {"non_english_text": True}}
        with mock.patch.object(llm, "complete_json", side_effect=lambda t, p, v: (v(dict(ans)), "x")):
            self.assertEqual(check.run(campaigns.get(cid))["verdict"], "no")
            extra = campaigns._extra(campaigns.get(cid)); extra["captions"] = "off"
            db.x("UPDATE campaigns SET notes=?, paused=0 WHERE id=?", json.dumps(extra), cid)
            self.assertEqual(check.run(campaigns.get(cid))["verdict"], "partial")

    def test_own_lanes_need_no_check(self):
        from clipfactory import campaigns, check
        campaigns.save("own-gta6", {"name": "GTA 6", "platforms": ["youtube"]}, "yaml")
        self.assertTrue(check.ready(campaigns.get("own-gta6")))

    def test_guide_text_is_kept_for_the_check(self):
        from clipfactory import campaigns, check, guide, llm
        doc = "Guide doc: profile picture must not show the game logo. Clips 20-40 seconds."
        data = ({"do": ["Show kills"], "avoid": ["Logos"], "hashtags": [], "credit": "", "clip_length": None}, "x")
        with mock.patch.object(llm, "complete_json", return_value=data):
            guide.add("cod", doc)
        self.assertIn("must not show the game logo", campaigns._extra(self.c())["guide_text"])
        self.assertIn("must not show the game logo", check._texts(self.c()))


if __name__ == "__main__":
    unittest.main()
