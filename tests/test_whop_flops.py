"""Whop link read without login, clips with few views (more + new versions), submit reminders. No network.

Run:  python -m unittest tests.test_whop_flops
"""
import datetime as dt
import os
import tempfile
import unittest
from unittest import mock

PAGE = """<html><head><title>x</title></head><body><h1>Kitsch x Creator Clipping</h1>
<p>Earn $1.50 per 1K views. Budget $20,000. Max payout $350 per clip.</p>
<p>Clips must be edited. Caption must tag @kitsch on Instagram and YouTube. 40%+ Tier 1 audience (USA).</p>
<a href="https://docs.google.com/document/d/GUIDEDOC1234567890abcdef/edit">Brief</a>
<a href="https://www.dropbox.com/scl/fo/abc123/footage?rlkey=x">Footage</a>
<script id="__NEXT_DATA__">{"props": {"brief": "Show the product in the first 3 seconds of every clip, please."}}</script>
</body></html>"""


class Resp:
    def __init__(self, text, url="https://whop.com/kitsch/clips/", status=200):
        self.text, self.url, self.status_code = text, url, status
        self.headers = {"content-type": "text/html; charset=utf-8"}


class WhopPageTest(unittest.TestCase):
    def test_page_text_links_and_hidden_brief(self):
        from clipfactory import whop_page
        with mock.patch("requests.get", return_value=Resp(PAGE)):
            text = whop_page.fetch("https://whop.com/kitsch/clips/")
        self.assertIn("Max payout $350", text)
        self.assertIn("Show the product in the first 3 seconds", text)   # from the page's data script
        self.assertIn("docs.google.com/document/d/GUIDEDOC1234567890abcdef", text)
        self.assertIn("dropbox.com/scl/fo/abc123", text)

    def test_login_wall_asks_for_a_paste(self):
        from clipfactory import whop_page
        with mock.patch("requests.get", return_value=Resp("<html>Sign in</html>", url="https://whop.com/login")):
            with self.assertRaises(whop_page.NeedsPaste) as e:
                whop_page.fetch("https://whop.com/kitsch/clips/")
        self.assertIn("Ctrl+A", str(e.exception))

    def test_no_cookies_or_login_sent(self):
        from clipfactory import whop_page
        with mock.patch("requests.get", return_value=Resp(PAGE)) as get:
            whop_page.fetch("https://whop.com/kitsch/clips/")
        kw = get.call_args.kwargs
        self.assertNotIn("cookies", kw)
        self.assertNotIn("Cookie", kw["headers"])
        self.assertNotIn("Authorization", kw["headers"])


class LaneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import campaigns, db
        db._local.__dict__.clear()
        campaigns.save("k", {"name": "Kitsch", "platforms": ["youtube", "instagram"], "rate_per_1k": 1.5,
                             "max_payout_per_clip": 350, "rules": []}, "pasted")
        db.x("UPDATE campaigns SET stage='active' WHERE id='k'")

    def tearDown(self):
        self.tmp.cleanup()

    def _clip(self, views, hours_ago=60, style="word_pop"):
        from clipfactory import db
        from clipfactory.util import iso, utcnow
        db.x("INSERT INTO moments(video_id, campaign_id, start_s, end_s, score, category, hook) VALUES('v','k',10,40,8,'funny','WOW')")
        mid = db.one("SELECT MAX(id) id FROM moments")["id"]
        db.x("INSERT INTO clips(moment_id, campaign_id, raw_path, title, style) VALUES(?,?,?,?,?)", mid, "k", "a_raw.mp4",
             "Old title", style)
        cid = db.one("SELECT MAX(id) id FROM clips")["id"]
        t = iso(utcnow() - dt.timedelta(hours=hours_ago))
        db.x("""INSERT INTO posts(clip_id, campaign_id, platform, account, status, posted_at, stats_at, views, url)
                VALUES(?,?,?,?,?,?,?,?,?)""", cid, "k", "youtube", "main", "posted", t, t, views, "https://youtu.be/x")
        return cid, mid

    def c(self):
        from clipfactory import campaigns
        return campaigns.get("k")

    def test_flops_raise_the_quota_and_make_new_versions(self):
        from clipfactory import db, flops, notify
        flop, mid = self._clip(views=40)
        self._clip(views=5000)            # a good one
        self._clip(views=10, hours_ago=5)  # too new to judge
        self.assertEqual(flops.flopped(self.c()), [flop])
        self.assertEqual(flops.quota(self.c()), (self.c()["daily_clip_quota"] or 3) + 1)
        with mock.patch.object(notify, "send") as sent:
            flops.run()
            flops.run()                    # the same flop is handled once
        self.assertEqual(sent.call_count, 1)
        self.assertIn("new version", sent.call_args.args[1])
        new = db.one("SELECT * FROM moments WHERE id != ? AND why LIKE 'New version%'", mid)
        self.assertEqual(new["status"], "found")
        self.assertIn("word_pop", db.kv_get(f"styles_used:{new['id']}"))
        self.assertIn("Old title", db.kv_get(f"title_hint:{new['id']}"))

    def test_no_new_versions_when_reposts_are_forbidden(self):
        from clipfactory import db, flops, notify
        db.x("""UPDATE campaigns SET rules='["No reposts of the same clip"]' WHERE id='k'""")
        self._clip(views=40)
        with mock.patch.object(notify, "send") as sent:
            flops.run()
        self.assertIn("doesn't allow reposts", sent.call_args.args[1])
        self.assertIsNone(db.one("SELECT 1 FROM moments WHERE why LIKE 'New version%'"))

    def test_min_views_sets_the_bar(self):
        from clipfactory import db, flops
        db.x("UPDATE campaigns SET min_views=1000 WHERE id='k'")
        flop, _ = self._clip(views=800)
        self.assertEqual(flops.flopped(self.c()), [flop])

    def test_submit_reminder_without_a_saved_link(self):
        from clipfactory import notify, publish
        clip, _ = self._clip(views=0, hours_ago=0)
        from clipfactory import db
        pid = db.one("SELECT id FROM posts WHERE clip_id=?", clip)["id"]
        with mock.patch.object(notify, "send") as sent:
            publish.submit_reminder(pid)
        text = sent.call_args.args[1]
        self.assertIn("Kitsch campaign page on Whop", text)
        self.assertNotIn("within 30 minutes", text)

    def test_running_lanes_get_the_check_without_new_videos(self):
        from clipfactory import check, llm, notify, pipeline
        from clipfactory import db
        db.x("UPDATE campaigns SET brief=? WHERE id='k'", "Kitsch campaign. Clips must be edited. " * 5)
        go = ({"verdict": "go", "summary": "Edited product clips.", "can": ["Cut clips"], "cannot": [],
               "account_rules": [], "owner_todo": []}, "fake")
        with mock.patch.object(llm, "complete_json", return_value=go), mock.patch.object(notify, "send"):
            pipeline._keep_up(self.c())
        self.assertEqual(check.get(self.c())["verdict"], "go")

    def test_whop_link_for_a_joined_campaign_reads_the_page(self):
        from clipfactory import joinflow, whop_page
        with mock.patch.object(whop_page, "fetch", return_value="PAGE TEXT " * 50), \
                mock.patch.object(joinflow, "apply_brief", return_value="read") as ab:
            out = joinflow.add_link("k", "https://whop.com/kitsch/clips/")
        self.assertEqual(out, "read")
        self.assertIn("Campaign page: https://whop.com/kitsch/clips/", ab.call_args.args[1])
        from clipfactory import campaigns
        self.assertEqual(campaigns._extra(self.c())["whop_url"], "https://whop.com/kitsch/clips/")


if __name__ == "__main__":
    unittest.main()


class FootagePasteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import campaigns, db, joinflow
        db._local.__dict__.clear()
        campaigns.save("kn", {"name": "Kitsch x Nic Vans", "platforms": ["instagram"], "rules": []}, "pasted")
        db.x("UPDATE campaigns SET stage='active' WHERE id='kn'")
        joinflow.expect("kn", "source")

    def tearDown(self):
        self.tmp.cleanup()

    def test_whole_post_is_read_like_the_page(self):
        from clipfactory import commands, joinflow
        post = ("Kitsch x Nic Vans clipping. Tag @kitsch on Instagram.\nFollow https://instagram.com/kitsch\n"
                "Footage: https://www.dropbox.com/scl/fo/abc/x?rlkey=1\n" + "Rules: clips must be edited.\n" * 20)
        with mock.patch.object(joinflow, "apply_brief", return_value="read the page") as b:
            self.assertEqual(commands.pasted("kn", post), "read the page")
            joinflow.expect("kn", "source")
            self.assertEqual(commands.handle("!source kn " + post), "read the page")   # /source with a post
            self.assertEqual(commands.handle("!source " + post), "read the page")      # no id: the waiting one
        self.assertEqual(b.call_count, 3)

    def test_two_links_after_i_joined(self):
        from clipfactory import commands, joinflow
        with mock.patch.object(joinflow, "add_link", return_value="ok") as al:
            commands.brief("kn", "https://www.dropbox.com/scl/fo/a/x?rlkey=1 https://www.twitch.tv/videos/123456")
        self.assertEqual(al.call_count, 2)

    def test_whop_link_is_kept_and_social_links_skipped(self):
        from clipfactory import commands, joinflow
        with mock.patch.object(joinflow, "add_link", return_value="ok") as al:
            out = commands.pasted("kn", "https://whop.com/kitsch/clips/ https://instagram.com/kitsch")
        al.assert_called_once_with("kn", "https://whop.com/kitsch/clips/")
        self.assertIn("Skipped 1 social media link", out)

    def test_only_social_links_get_a_plain_answer(self):
        from clipfactory import commands
        out = commands.pasted("kn", "https://www.tiktok.com/@kitsch/video/123")
        self.assertIn("not footage", out)
