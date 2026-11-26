"""Reference material behind any link: public Notion pages (sub-pages, uploaded videos) and web pages, opened level
by level until nothing new opens. No network.

Run:  python -m unittest tests.test_deep_links
"""
import os
import tempfile
import unittest
from unittest import mock

ROOT = "11111111-1111-1111-1111-111111111111"
SUB = "22222222-2222-2222-2222-222222222222"


def block(bid, typ, title=None, content=(), **kw):
    props = {"title": title} if title is not None else {}
    props.update(kw.pop("props", {}))
    return {bid: {"value": {"value": {"id": bid, "type": typ, "properties": props, "content": list(content),
                                      "space_id": "sp", **kw}}}}


def notion_post(endpoint, body):
    pid = body.get("pageId")
    blocks = {}
    if pid == ROOT:
        blocks.update(block(ROOT, "page", [["Lucia campaign"]], ["h1", "t1", SUB, "tg"]))
        blocks.update(block("h1", "header", [["What to post"]]))
        blocks.update(block("t1", "text", [["Read the formats, then the "], ["assets", [["a", "https://brand.example/assets"]]]]))
        blocks.update(block(SUB, "page", [["Reference material"]]))
        blocks.update(block("tg", "toggle", [["Rules"]], ["r1"]))   # its child "r1" comes from syncRecordValues
    elif pid == SUB:
        blocks.update(block(SUB, "page", [["Reference material"]], ["v1", "e1"]))
        blocks.update(block("v1", "video", props={"source": [["attachment:abc:ai_lucia_1.mp4"]]}))
        blocks.update(block("e1", "file", props={"source": [["attachment:def:end_card.png"]]}))
    elif endpoint == "syncRecordValues":
        blocks.update(block("r1", "bulleted_list", [["No voice-over. Use the end card."]]))
    return {"recordMap": {"block": blocks}, "cursor": {"stack": []}}


class DeepLinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_notion_page_reads_toggles_and_finds_sub_pages_and_files(self):
        from clipfactory import notion
        with mock.patch.object(notion, "_post", side_effect=notion_post):
            page = notion.read(f"https://app.notion.com/p/Lucia-{ROOT.replace('-', '')}?pvs=4")
        self.assertEqual(page["title"], "Lucia campaign")
        self.assertIn("# What to post", page["text"])
        self.assertIn("No voice-over. Use the end card.", page["text"])          # inside a closed toggle
        self.assertIn(notion.page_url(SUB), page["links"])                         # the next level
        self.assertIn("https://brand.example/assets", page["links"])

    def test_explore_goes_level_by_level(self):
        from clipfactory import campaigns, gdrive, joinflow, llm, notion, watcher, webpage
        cid = campaigns.suggest({"name": "Lucia", "platforms": ["instagram"], "rate_per_1k": 1.5}, "pasted")
        joinflow.joined(cid)
        pages = {"https://brand.example/assets": {
            "title": "Brand assets", "url": "https://brand.example/assets",
            "text": "[Web page: Brand assets]\n" + "Use the logo at the end of every clip. " * 5,
            "links": ["https://brand.example/assets/examples", "https://www.instagram.com/p/xyz/",
                      "https://brand.example/login"]},
            "https://brand.example/assets/examples": {
                "title": "Examples", "url": "https://brand.example/assets/examples",
                "text": "[Web page: Examples]\n" + "Five formats that work: reaction, side by side. " * 3,
                "links": ["https://www.youtube.com/watch?v=abcdefghijk"]}}
        opened = []

        def read_web(u):
            opened.append(u)
            return pages[u]
        with mock.patch.object(notion, "_post", side_effect=notion_post), \
                mock.patch.object(notion, "download", side_effect=lambda u, out: out.write_bytes(b"v")) as dl, \
                mock.patch.object(webpage, "read", side_effect=read_web), \
                mock.patch.object(watcher, "add_video", return_value="yt1") as vid, \
                mock.patch.object(llm, "complete_json", return_value=({"do": ["x"], "avoid": []}, "fake")) as ai:
            out = gdrive.explore(cid, f"https://lucia.notion.site/Lucia-{ROOT.replace('-', '')}", drive=object())
        self.assertEqual(opened, ["https://brand.example/assets", "https://brand.example/assets/examples"])
        self.assertEqual(dl.call_count, 1)                                         # the video, not the png
        srcs = {s["kind"] for s in campaigns.sources(cid)}
        self.assertIn("folder", srcs)                                              # Notion videos → footage
        vid.assert_called_once_with("https://www.youtube.com/watch?v=abcdefghijk", cid)   # 3 levels down
        prompt = ai.call_args[0][1]
        for t in ("No voice-over", "Use the logo", "Five formats"):
            self.assertIn(t, prompt)
        self.assertIn("Read 4 page(s)", out)
        self.assertEqual(campaigns.get(cid)["stage"], "active")                    # footage found → running

    def test_social_login_and_campaign_site_links_are_never_opened(self):
        from clipfactory import webpage
        for u in ("https://www.tiktok.com/@x/video/1", "https://brand.example/login", "https://whop.com/x",
                  "https://contentrewards.com/c/campaigns/abc", "https://brand.example/logo.png"):
            self.assertFalse(webpage.worth_opening(u, "https://brand.example/", 1), u)
        self.assertTrue(webpage.worth_opening("https://other.test/media-kit", None, 3))
        self.assertFalse(webpage.worth_opening("https://other.test/blog/post", "https://brand.example/", 3))

    def test_notion_database_rows_are_the_next_level(self):
        from clipfactory import notion
        db_block = {"id": "cv", "type": "collection_view", "collection_id": "col", "view_ids": ["view"],
                    "space_id": "sp"}

        def post(endpoint, body):
            self.assertEqual(endpoint, "queryCollection")
            return {"result": {"reducerResults": {"collection_group_results": {"blockIds": ["r1", "r2"]}}},
                    "recordMap": {"block": {**block("r1", "page", [["RULES "]]), **block("r2", "page", [["Formats"]])}}}
        with mock.patch.object(notion, "_post", side_effect=post):
            self.assertEqual(notion._rows(db_block), [("r1", "RULES"), ("r2", "Formats")])

    def test_end_card_and_example_folders_are_assets_not_footage(self):
        from clipfactory import campaigns, gdrive, joinflow, llm

        class D:
            ready = True
            names = {"F1aaaaaaaaaaaa": "AI Lucia clips", "F2aaaaaaaaaaaa": "End card + NY Post headline",
                     "F3aaaaaaaaaaaa": "Format example videos"}

            def meta(self, fid):
                return {"id": fid, "name": self.names[fid], "mimeType": "application/vnd.google-apps.folder"}

            def children(self, fid):
                return [{"id": fid + "v", "name": "a.mp4", "mimeType": "video/mp4", "size": "5"}]

            def text(self, item):
                return ""
        cid = campaigns.suggest({"name": "Lucia", "platforms": ["instagram"], "rate_per_1k": 1.5}, "pasted")
        joinflow.joined(cid)
        with mock.patch.object(gdrive, "LINK", gdrive.LINK), \
                mock.patch.object(llm, "complete_json", return_value=({"do": [], "avoid": []}, "fake")):
            for f in D.names:
                out = gdrive.explore(cid, f"https://drive.google.com/drive/folders/{f}", drive=D())
        srcs = {s["channel_id"] for s in campaigns.sources(cid)}
        self.assertEqual(srcs, {"folder:F1aaaaaaaaaaaa"})                          # only the clips to cut from
        self.assertEqual(set(campaigns._extra(campaigns.get(cid))["assets"]),
                         {"End card + NY Post headline", "Format example videos"})
        self.assertIn("🧩 Assets", out)


if __name__ == "__main__":
    unittest.main()
