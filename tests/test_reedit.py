"""🔁 Re-edit: buttons and typed notes make the clip again; live clips are left alone. No network.

Run:  python -m unittest tests.test_reedit
"""
import json
import os
import tempfile
import unittest
from unittest import mock


class ReeditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _clip(self, status="pending_review"):
        from clipfactory import campaigns, db
        cid = campaigns.suggest({"name": "Band Live", "platforms": ["instagram"], "rate_per_1k": 2,
                                 "clip_length": [12, 30]}, "pasted")
        campaigns.activate(cid)
        db.x("INSERT INTO videos(id, campaign_id, title, status, duration_s) VALUES(?,?,?,?,?)", "v1", cid, "a", "done", 600)
        m = db.x("INSERT INTO moments(video_id, campaign_id, start_s, end_s, score, category, hook, why, status) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", "v1", cid, 100, 120, 8, "reaction", "crowd", "loud", "downloaded")
        k = db.x("INSERT INTO clips(moment_id, campaign_id, raw_path, final_path, titles, title, words, status) "
                 "VALUES(?,?,?,?,?,?,?,?)", m, cid, "raw.mp4", "final.mp4", json.dumps(["OLD TITLE"]), "OLD TITLE",
                 json.dumps([{"w": "wow"}]), status)
        return cid, m, k

    def test_button_shows_choices(self):
        from clipfactory import commands, db
        _, _, k = self._clip()
        self.assertIn("What should change?", commands.button(f"re:{k}"))
        card = db.q("SELECT * FROM outbox")[-1]
        self.assertIn(f"Re-edit clip #{k}", card["text"])
        self.assertEqual(json.loads(card["buttons"])[1][1], f"ed:{k}:earlier")

    def test_start_earlier_recuts_the_moment(self):
        from clipfactory import commands, db
        _, m, k = self._clip()
        out = commands.button(f"ed:{k}:earlier")
        self.assertIn("being cut again (97s → 120s", out)
        mo = db.one("SELECT * FROM moments WHERE id=?", m)
        self.assertEqual((mo["start_s"], mo["status"]), (97, "found"))
        self.assertEqual(db.one("SELECT status FROM clips WHERE id=?", k)["status"], "rejected")

    def test_new_titles_with_feedback(self):
        from clipfactory import commands, db, llm
        _, _, k = self._clip()
        new = ({"titles": ["NEW ONE", "NEW TWO", "NEW THREE"], "description": "d", "hashtags": ["#music"]}, "fake")
        with mock.patch.object(llm, "complete_json", return_value=new) as m:
            out = commands.button(f"ed:{k}:titles")
        self.assertIn("new titles", out)
        self.assertIn("Write clearly different titles from: OLD TITLE", m.call_args[0][1])
        self.assertEqual(db.one("SELECT title FROM clips WHERE id=?", k)["title"], "NEW ONE")

    def test_typed_note_goes_through_the_free_ai(self):
        from clipfactory import commands, db, joinflow, llm
        cid, m, k = self._clip()
        commands.button(f"re:{k}")
        self.assertEqual(joinflow.awaiting_kind(), "reedit")
        ai = ({"start_shift": -5, "end_shift": 4, "new_titles": True, "title_hint": "shorter, punchier",
               "different_moment": False}, "fake")
        with mock.patch.object(llm, "complete_json", return_value=ai):
            out = commands.pasted(cid, "start when the crowd screams and make the title shorter")
        self.assertIn("being cut again (95s → 124s", out)
        self.assertEqual(db.kv_get(f"title_hint:{m}"), "shorter, punchier")
        from clipfactory import reedit
        self.assertEqual(reedit.consume_hint(m), "shorter, punchier")
        self.assertEqual(reedit.consume_hint(m), "")

    def test_live_clip_cannot_be_reedited(self):
        from clipfactory import commands, db
        cid, _, k = self._clip("scheduled")
        db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, status) VALUES(?,?,?,?,?)",
             k, cid, "instagram", "main", "posted")
        self.assertIn("already live", commands.button(f"ed:{k}:later"))

    def test_approve_says_where_and_when_and_queue_lists_it(self):
        from clipfactory import commands, db
        cid, _, k = self._clip()
        db.x("UPDATE clips SET style='meme_top' WHERE id=?", k)
        with __import__("unittest.mock").mock.patch("clipfactory.publish.platform_ready", return_value=True), \
                __import__("unittest.mock").mock.patch("clipfactory.publish.mode", return_value="auto"):
            out = commands.approve(k)
        self.assertIn("approved. It goes out:", out)
        self.assertIn("• Instagram:", out)
        self.assertIn("posts by itself", out)
        self.assertIn(f"clip #{k}", commands.handle("/queue"))

    def test_review_card_has_reedit_button(self):
        from clipfactory import db, review
        _, _, k = self._clip()
        db.x("UPDATE clips SET style='blur_box' WHERE id=?", k)
        review.notify(k)
        self.assertIn("Style: Blurred background + title box", db.q("SELECT text FROM outbox")[-1]["text"])
        btn = json.loads(db.q("SELECT buttons FROM outbox")[-1]["buttons"])
        self.assertIn(["🔁 Re-edit", f"re:{k}", "grey"], btn)


if __name__ == "__main__":
    unittest.main()
