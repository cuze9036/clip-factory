"""Google Drive: one link -> guides read, links inside followed, video folders added and downloaded. No network.

Run:  python -m unittest tests.test_gdrive
"""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

GUIDE = ("EDITING GUIDE\nAlways show the band name. No sponsor parts.\n"
         "Raw footage: https://drive.google.com/drive/folders/VIDEOS1234567890abc?usp=sharing\n"
         "Extra: https://www.youtube.com/@BandOfficial")
DOCMIME = "application/vnd.google-apps.document"
FOLDERMIME = "application/vnd.google-apps.folder"


class FakeDrive:
    ready = True
    creds = object()

    def __init__(self):
        self.tree = {
            "VIDEOS1234567890abc": [{"id": "v1", "name": "show1.mp4", "mimeType": "video/mp4", "size": "5"},
                                     {"id": "sub1234567890", "name": "Day 2", "mimeType": FOLDERMIME},
                                     {"id": "rules1234567890", "name": "Rules", "mimeType": DOCMIME}],
            "sub1234567890": [{"id": "v2", "name": "show2.mov", "mimeType": "video/quicktime", "size": "5"}],
        }
        self.docs = {"GUIDEDOC1234567890abcdef": GUIDE, "rules1234567890": "Clips must be 15-40 seconds."}
        self.downloads = []

    def meta(self, fid):
        if fid in self.docs:
            return {"id": fid, "name": "Guide", "mimeType": DOCMIME}
        if fid in self.tree:
            return {"id": fid, "name": "Footage", "mimeType": FOLDERMIME}
        return {"id": fid, "name": fid + ".mp4", "mimeType": "video/mp4", "size": "5"}

    def children(self, fid):
        return self.tree.get(fid, [])

    def text(self, item):
        return self.docs.get(item["id"], "")

    def download(self, fid, dest, busy=None):
        self.downloads.append(fid)
        dest.write_bytes(b"x" * 5)


GUIDE_AI = ({"do": ["Always show the band name"], "avoid": ["No sponsor parts"], "hashtags": [], "credit": "",
             "clip_length": [15, 40]}, "fake")


class DriveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _campaign(self):
        from clipfactory import campaigns, joinflow
        cid = campaigns.suggest({"name": "Band Live", "platforms": ["instagram"], "rate_per_1k": 2}, "pasted")
        joinflow.joined(cid)
        return cid

    def test_one_guide_link_finds_everything(self):
        from clipfactory import campaigns, gdrive, llm
        cid = self._campaign()
        with mock.patch.object(llm, "complete_json", return_value=GUIDE_AI) as m:
            out = gdrive.explore(cid, "https://docs.google.com/document/d/GUIDEDOC1234567890abcdef/edit", drive=FakeDrive())
        srcs = {s["channel_id"]: s["kind"] for s in campaigns.sources(cid)}
        self.assertEqual(srcs.get("folder:VIDEOS1234567890abc"), "gdrive")
        self.assertEqual(srcs.get("folder:sub1234567890"), "gdrive")
        self.assertEqual(srcs.get("@BandOfficial"), "youtube_channel")      # a link inside the guide
        prompt = m.call_args[0][1]
        self.assertIn("Always show the band name", prompt)                   # the guide doc
        self.assertIn("Clips must be 15-40 seconds", prompt)                 # the doc inside the videos folder
        self.assertIn("🎬 Footage: 2 video(s) in 2 folder(s)", out)
        self.assertIn("📋 Guide read from 2 document(s)", out)
        self.assertIn("is running", out)
        self.assertEqual(campaigns.get(cid)["stage"], "active")

    def test_private_without_login_explains_the_one_step(self):
        from clipfactory import gdrive

        class NoAccess(FakeDrive):
            def children(self, fid):
                raise gdrive.NeedsLogin(gdrive.LOGIN_HELP)

        cid = self._campaign()
        out = gdrive.explore(cid, "https://drive.google.com/drive/folders/VIDEOS1234567890abc", drive=NoAccess())
        self.assertIn("clipfactory auth-drive main", out)

    def test_sync_downloads_only_new_videos(self):
        from clipfactory import campaigns, gdrive
        cid = self._campaign()
        gdrive._add_source(cid, "folder:VIDEOS1234567890abc")
        s = campaigns.sources(cid)[0]
        root = pathlib.Path(self.tmp.name) / "g"
        d = FakeDrive()
        self.assertEqual(gdrive.sync(campaigns.get(cid), s, root, drive=d), 2)   # includes the subfolder
        self.assertEqual(gdrive.sync(campaigns.get(cid), s, root, drive=d), 0)
        self.assertEqual(sorted(d.downloads), ["v1", "v2"])

    def test_footage_button_link_goes_to_the_explorer(self):
        from clipfactory import commands, joinflow
        cid = self._campaign()
        joinflow.expect(cid, "source")
        with mock.patch("clipfactory.gdrive.explore", return_value="explored") as ex:
            out = commands.pasted(cid, "https://drive.google.com/drive/folders/VIDEOS1234567890abc?usp=sharing")
        ex.assert_called_once()
        self.assertIn("explored", out)

    def test_watcher_downloads_then_clips(self):
        from clipfactory import campaigns, db, gdrive, watcher
        cid = self._campaign()
        gdrive._add_source(cid, "folder:VIDEOS1234567890abc")
        campaigns.activate(cid)
        with mock.patch.object(gdrive, "Drive", FakeDrive), \
                mock.patch.object(watcher, "ffprobe", return_value={"duration": 300}):
            watcher.check_sources()
            watcher.check_sources()
        v = db.q("SELECT status FROM videos WHERE campaign_id=?", cid)
        self.assertEqual(sorted(r["status"] for r in v), ["new", "new"])


if __name__ == "__main__":
    unittest.main()


class BlockedLinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_links_it_cant_open_are_explained_simply(self):
        from clipfactory import campaigns, gdrive, joinflow, llm, notify
        cid = campaigns.suggest({"name": "CoD Clips", "platforms": ["youtube"], "rate_per_1k": 1}, "pasted")
        joinflow.joined(cid)
        d = FakeDrive()
        d.docs["GUIDEDOC1234567890abcdef"] = ("Guide. Footage: https://mega.nz/folder/abc#key and "
                                              "https://drive.google.com/drive/folders/PRIVATE1234567890")
        real = d.children

        def children(fid):
            if fid == "PRIVATE1234567890":
                raise gdrive.NeedsLogin("private")
            return real(fid)
        d.children = children
        with mock.patch.object(llm, "complete_json", return_value=GUIDE_AI), \
                mock.patch.object(notify, "send") as sent:
            out = gdrive.explore(cid, "https://docs.google.com/document/d/GUIDEDOC1234567890abcdef/edit", drive=d)
        note = [c.args[1] for c in sent.call_args_list if "couldn't open" in c.args[1]][0]
        self.assertIn("2 link(s)", note)
        self.assertIn("I can't download from mega.nz", note)
        self.assertIn("Anyone with the link", note)
        self.assertIn("🔒", out)
        self.assertEqual(len(campaigns._extra(campaigns.get(cid))["blocked_links"]), 2)

    def test_twitch_clips_and_vods_are_footage(self):
        from clipfactory import campaigns
        for u in ("https://clips.twitch.tv/FunnyClipSlug", "https://www.twitch.tv/somechannel/clip/FunnySlug-abc",
                  "https://www.twitch.tv/videos/123456789"):
            self.assertEqual(campaigns.parse_source(u)[0], "video", u)
        self.assertEqual(campaigns.parse_source("https://www.twitch.tv/somechannel")[0], "twitch_channel")
