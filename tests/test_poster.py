"""A helper posts TikTok / X clips from a Discord channel: video + caption there, ✅ Posted asks the link, the link
goes to the owner's submit flow. No network.

Run:  python -m unittest tests.test_poster
"""
import datetime as dt
import os
import tempfile
import unittest
from unittest import mock


class PosterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _post(self, platform="tiktok"):
        from clipfactory import campaigns, db
        from clipfactory.util import iso, utcnow
        cid = campaigns.suggest({"name": "Lucia", "platforms": [platform], "rate_per_1k": 1.5}, "pasted")
        video = os.path.join(self.tmp.name, "final.mp4")
        with open(video, "wb") as f:
            f.write(b"v")
        k = db.x("INSERT INTO clips(campaign_id, status, title, description, final_path) VALUES(?,?,?,?,?)",
                 cid, "scheduled", "GTA 6 Lucia", "she's AI #gta6", video)
        return db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, scheduled_at, status, mode) "
                    "VALUES(?,?,?,?,?,?,?)", k, cid, platform, "main", iso(utcnow() + dt.timedelta(hours=2)),
                    "queued", "manual")

    def _cfg(self, **kw):
        from clipfactory import config
        real = config.section
        return mock.patch.object(config, "section", side_effect=lambda name: (
            {"channel_id": "123", "helper_ids": [], "platforms": ["tiktok", "x"], **kw} if name == "poster"
            else real(name)))

    def test_hand_posts_go_to_the_helper_channel_not_telegram(self):
        from clipfactory import db, publish, telegram
        pid = self._post()
        with self._cfg(), mock.patch.object(telegram, "hand_off") as tg, \
                mock.patch.object(publish, "_ready_files", return_value=("#1", "she's AI #gta6")):
            publish.deliver(db.one("SELECT * FROM posts WHERE id=?", pid), db.one("SELECT * FROM clips"))
        tg.assert_not_called()
        rows = db.q("SELECT * FROM outbox WHERE kind='poster' ORDER BY id")
        self.assertEqual(len(rows), 2)
        self.assertEqual(__import__("json").loads(rows[0]["buttons"])[0][:2], ["✅ Posted", f"pp:{pid}"])
        self.assertTrue(rows[0]["file_path"].endswith("final.mp4"))
        self.assertEqual(rows[1]["text"], "she's AI #gta6")                       # the caption alone, easy to copy
        self.assertTrue(db.one("SELECT delivered_at FROM posts WHERE id=?", pid)["delivered_at"])

    def test_link_marks_it_live_and_the_owner_gets_submit_now(self):
        from clipfactory import db, poster
        pid = self._post()
        self.assertIn("isn't a TikTok link", poster.posted(pid, "https://x.com/a/status/1"))
        reply = poster.posted(pid, "done! https://www.tiktok.com/@jay.plays/video/7712345678901234567 ", "Ravi")
        self.assertTrue(reply.startswith("✅ Saved post"))
        p = db.one("SELECT * FROM posts WHERE id=?", pid)
        self.assertEqual((p["status"], p["url"]), ("posted", "https://www.tiktok.com/@jay.plays/video/7712345678901234567"))
        self.assertTrue(db.q("SELECT 1 FROM outbox WHERE kind='submit' AND post_id=?", pid))   # 📌 Submit now
        self.assertIn("already saved", poster.posted(pid, "https://www.tiktok.com/@x/video/1"))

    def test_reply_to_the_video_finds_its_post_and_cant_post_tells_the_owner(self):
        from clipfactory import db, notify, poster
        pid = self._post("x")
        row = notify.send("poster", "video", post_id=pid)
        notify.mark_sent(row, "999")
        self.assertEqual(poster.post_for_message(999), pid)
        self.assertIn("skipped", poster.cant(pid, "Ravi"))
        self.assertTrue(db.q("SELECT 1 FROM outbox WHERE kind='manual' AND text LIKE '%Ravi couldn''t post%'"))

    def test_only_listed_helpers_may_press(self):
        from clipfactory import poster
        with self._cfg(helper_ids=["42"]):
            self.assertTrue(poster.may_use(42, 1))
            self.assertTrue(poster.may_use(1, 1))      # the owner
            self.assertFalse(poster.may_use(7, 1))
        with self._cfg(channel_id=""):
            self.assertFalse(poster.handles("tiktok"))   # not set up: Telegram as before


    def test_setup_saves_the_ids_in_config_local(self):
        import pathlib
        import yaml
        from clipfactory import config, poster
        local = pathlib.Path(self.tmp.name) / "config.local.yaml"
        local.write_text("render:\n  style: split\n", encoding="utf-8")
        answers = iter(["  1234567890123456789 ", "<@987654321098765432>"])
        with mock.patch.object(config, "ROOT", pathlib.Path(self.tmp.name)):
            self.assertEqual(poster.setup(ask=lambda _: next(answers)), 0)
        data = yaml.safe_load(local.read_text(encoding="utf-8"))
        self.assertEqual(data["render"], {"style": "split"})                       # other settings kept
        self.assertEqual(data["poster"]["channel_id"], "1234567890123456789")
        self.assertEqual(data["poster"]["helper_ids"], [987654321098765432])


if __name__ == "__main__":
    unittest.main()
