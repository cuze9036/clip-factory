"""Dropbox share links (no account needed) and the campaign guide from reference material. No network.

Run:  python -m unittest tests.test_dropbox_guide
"""
import io
import os
import pathlib
import tempfile
import unittest
import zipfile
from unittest import mock


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class _Resp:
    def __init__(self, body, ctype="application/zip", status=200, disp='attachment; filename="Campaign.zip"'):
        self.body, self.status_code = body, status
        self.headers = {"content-type": ctype, "content-disposition": disp}

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


GUIDE = ({"do": ["Start on the loudest crowd moment", "Show the band name on screen"],
          "avoid": ["No clips from the sponsor segment"], "hashtags": ["#BandAd"], "credit": "@band",
          "clip_length": [10, 45]}, "fake")


class DropboxGuideTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _campaign(self):
        from clipfactory import campaigns
        return campaigns.suggest({"name": "Band Live", "platforms": ["youtube"], "rate_per_1k": 2}, "pasted")

    def test_direct_link_keeps_rlkey(self):
        from clipfactory import watcher
        self.assertEqual(watcher.dropbox_direct("https://www.dropbox.com/scl/fo/abc/xyz?rlkey=k1&dl=0"),
                         "https://www.dropbox.com/scl/fo/abc/xyz?rlkey=k1&dl=1")
        self.assertEqual(watcher.dropbox_direct("https://www.dropbox.com/sh/abc/def"),
                         "https://www.dropbox.com/sh/abc/def?dl=1")

    def test_parse_and_add_dropbox_source(self):
        from clipfactory import campaigns
        cid = self._campaign()
        self.assertEqual(campaigns.parse_source("https://www.dropbox.com/scl/fo/abc/xyz?rlkey=k1&dl=0")[0], "dropbox")
        self.assertIn("no Dropbox account", campaigns.add_source(cid, "https://www.dropbox.com/scl/fo/abc/xyz?rlkey=k1&dl=0"))
        self.assertEqual(campaigns.sources(cid)[0]["kind"], "dropbox")

    def test_folder_zip_download_keeps_videos_and_references_and_blocks_zip_slip(self):
        from clipfactory import watcher
        root = pathlib.Path(self.tmp.name) / "dbx"
        body = _zip({"Footage/show1.mp4": b"v" * 50, "Footage/notes.txt": "Clip the chorus", "../evil.mp4": b"x",
                     "Brand/logo.svg": b"<svg/>"})
        with mock.patch("requests.get", return_value=_Resp(body)):
            videos, refs = watcher.dropbox_download("https://www.dropbox.com/scl/fo/a/b?rlkey=k&dl=0", root)
        self.assertEqual([v.name for v in videos], ["show1.mp4"])
        self.assertEqual([r.name for r in refs], ["notes.txt"])
        self.assertFalse((pathlib.Path(self.tmp.name) / "evil.mp4").exists())
        with mock.patch("requests.get", return_value=_Resp(body)):   # second run: nothing new
            self.assertEqual(watcher.dropbox_download("https://www.dropbox.com/scl/fo/a/b?rlkey=k&dl=0", root), ([], []))

    def test_web_page_instead_of_files_is_a_clear_error(self):
        from clipfactory import watcher
        with mock.patch("requests.get", return_value=_Resp(b"<html>", ctype="text/html")):
            with self.assertRaises(RuntimeError) as e:
                watcher.dropbox_download("https://www.dropbox.com/scl/fo/a/b", pathlib.Path(self.tmp.name) / "d")
        self.assertIn("too big", str(e.exception))

    def test_watcher_downloads_then_clips_like_a_folder(self):
        from clipfactory import campaigns, db, llm, watcher
        cid = self._campaign()
        campaigns.add_source(cid, "https://www.dropbox.com/scl/fo/a/b?rlkey=k&dl=0")
        campaigns.activate(cid)
        body = _zip({"show1.mp4": b"v" * 50, "brief.txt": "Show the band name. No sponsor segment."})
        with mock.patch("requests.get", return_value=_Resp(body)), \
                mock.patch.object(watcher, "ffprobe", return_value={"duration": 300}), \
                mock.patch.object(llm, "complete_json", return_value=GUIDE):
            watcher.check_sources()      # downloads, file waits until its size is stable
            watcher.check_sources()      # same size: ready to clip (no second download within 6 h)
        v = db.q("SELECT * FROM videos WHERE campaign_id=?", cid)
        self.assertEqual([(r["title"], r["status"]) for r in v], [("show1", "new")])
        self.assertIn("Show the band name on screen", " ".join(campaigns._extra(campaigns.get(cid))["guide"]["do"]))

    def test_guide_command_and_prompts(self):
        from clipfactory import campaigns, commands, llm
        cid = self._campaign()
        commands.button(f"join:{cid}")
        with mock.patch.object(llm, "complete_json", return_value=GUIDE):
            out = commands.handle("!guide Clip the loudest crowd moments, always show the band name, never the sponsor.")
        self.assertIn("✅ Start on the loudest crowd moment", out)
        self.assertIn("⛔ No clips from the sponsor segment", out)
        c = campaigns.get(cid)
        self.assertEqual((c["min_len"], c["max_len"]), (10, 45))
        self.assertEqual(c["required_hashtags"], ["#BandAd"])
        self.assertEqual(c["credit"], "@band")
        block = llm.rules_block(c)
        self.assertIn("- Do: Show the band name on screen", block)
        self.assertIn("- Avoid: No clips from the sponsor segment", block)
        self.assertIn("Guide for Band Live", commands.handle(f"!guide {cid}"))

    def test_pasted_page_with_dropbox_link_adds_footage_and_guide(self):
        from clipfactory import campaigns, joinflow, llm
        cid = self._campaign()
        joinflow.joined(cid)
        page = "Footage: https://www.dropbox.com/scl/fo/a/b?rlkey=k&dl=0 Rules: show the band name. " * 2
        with mock.patch.object(campaigns, "extract_from_brief", return_value=({"name": "Band Live", "sources": []}, "f")), \
                mock.patch.object(llm, "complete_json", return_value=GUIDE):
            out = joinflow.apply_brief(cid, page)
        self.assertIn("Dropbox link", out)
        self.assertIn("Guide saved: 2 things to do", out)
        self.assertIn("The lane is running", out)
        self.assertEqual(len(campaigns.sources(cid)), 1)


    def _two_running(self):
        from clipfactory import campaigns
        a = campaigns.suggest({"name": "Band Live", "platforms": ["youtube"], "rate_per_1k": 2}, "pasted")
        b = campaigns.suggest({"name": "Podcast Clips", "platforms": ["youtube"], "rate_per_1k": 1}, "pasted")
        campaigns.activate(a)
        campaigns.activate(b)
        return a, b

    def test_card_buttons_send_the_paste_to_that_campaign_only(self):
        from clipfactory import campaigns, commands, llm
        a, b = self._two_running()
        out = commands.button(f"guide:{b}")
        self.assertIn("Now paste the guide / reference material for Podcast Clips", out)
        with mock.patch.object(llm, "complete_json", return_value=GUIDE):
            out = commands.pasted(b, "Clip the funniest guest stories, never the ad reads, keep it under 45 s.")
        self.assertIn("Guide for Podcast Clips", out)
        self.assertTrue(campaigns._extra(campaigns.get(b)).get("guide"))
        self.assertFalse(campaigns._extra(campaigns.get(a)).get("guide"))
        out = commands.button(f"src:{a}")
        self.assertIn("Footage for Band Live", out)
        out = commands.pasted(a, "https://www.dropbox.com/scl/fo/a/b?rlkey=k&dl=0")
        self.assertTrue(out.startswith("**Band Live**"))
        self.assertEqual(campaigns.sources(a)[0]["kind"], "dropbox")
        self.assertEqual(campaigns.sources(b), [])

    def test_unclear_campaign_asks_which(self):
        from clipfactory import commands
        a, b = self._two_running()
        out = commands.handle("!guide Clip the funniest guest stories and never the ad reads please.")
        self.assertIn("Which campaign?", out)
        self.assertIn(f"`{a}`", out)
        self.assertIn(f"`{b}`", out)
        self.assertIn("Which campaign?", commands.handle("!source https://www.youtube.com/@x"))
        with mock.patch("clipfactory.llm.complete_json", return_value=GUIDE):
            self.assertIn("Guide for Podcast Clips", commands.handle(f"!guide {b} Clip the funniest guest stories."))

    def test_pause_button_toggles_and_card_shows_it(self):
        from clipfactory import board, commands
        a, _ = self._two_running()
        self.assertIn("paused", commands.button(f"pause:{a}"))
        card = next(i for i in board.plan()["running"] if i["key"] == f"running:c:{a}")
        self.assertTrue(card["text"].startswith("**⏸ PAUSED**"))
        self.assertEqual(card["buttons"][2][0], "▶ Resume")
        self.assertIn("running again", commands.button(f"pause:{a}"))


    def _doc_resp(self, text, url="https://docs.google.com/document/d/x/export?format=txt", ctype="text/plain; charset=utf-8"):
        r = mock.Mock(status_code=200, text=text, url=url, headers={"content-type": ctype}, encoding="utf-8")
        return r

    def test_google_doc_link_is_read_into_the_guide(self):
        from clipfactory import campaigns, commands, guide, llm
        a, b = self._two_running()
        commands.button(f"guide:{a}")
        link = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit?usp=sharing"
        with mock.patch("requests.get", return_value=self._doc_resp("EDITING GUIDE: hook in 1 second, band name on screen")) as g, \
                mock.patch.object(llm, "complete_json", return_value=GUIDE) as m:
            out = commands.pasted(a, link)
        self.assertTrue(g.call_args[0][0].endswith("/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/export?format=txt"))
        self.assertIn("EDITING GUIDE: hook in 1 second", m.call_args[0][1])
        self.assertIn("Guide for Band Live", out)
        self.assertTrue(guide.get(campaigns.get(a))["do"])

    def test_private_google_doc_gives_clear_steps(self):
        from clipfactory import commands
        a, _ = self._two_running()
        commands.button(f"guide:{a}")
        login = self._doc_resp("<html>", url="https://accounts.google.com/ServiceLogin?x", ctype="text/html")
        with mock.patch("requests.get", return_value=login):
            out = commands.pasted(a, "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit")
        self.assertIn("Anyone with the link", out)
        self.assertIn("Plain text (.txt)", out)

    def test_slides_and_sheets_export_urls(self):
        from clipfactory import guide
        with mock.patch("requests.get", return_value=self._doc_resp("slide text")) as g:
            guide.fetch_doc("https://docs.google.com/presentation/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit#slide=id.p")
        self.assertTrue(g.call_args[0][0].endswith("/presentation/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/export/txt"))

    def test_text_file_attachment_is_read(self):
        import asyncio
        from clipfactory import discord_bot

        class Att:
            filename, content_type, size = "message.txt", "text/plain; charset=utf-8", 12

            async def read(self):
                return b"long guide text"

        msg = mock.Mock(content="", attachments=[Att()], embeds=[])
        self.assertEqual(asyncio.run(discord_bot.full_text(msg)), "long guide text")


if __name__ == "__main__":
    unittest.main()
