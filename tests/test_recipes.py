"""Campaign formats built from the campaign's own clips (recipes.py): the GTA VI Lucia "AI reveal". Uses FFmpeg.

Run:  python -m unittest tests.test_recipes
"""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock


def make(path, args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args, str(path)], check=True)


class RecipeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_ai_reveal_clip_is_built_with_the_end_card_on_the_last_seconds(self):
        from clipfactory import campaigns, db, pipeline, qa, recipes
        d = self.tmp.name
        short, other = os.path.join(d, "lucia1.mp4"), os.path.join(d, "lucia2.mp4")
        for p, sec in ((short, 5), (other, 9)):   # vertical clips with sound, like the campaign's
            make(p, ["-f", "lavfi", "-i", f"color=c=0x203040:s=720x1280:d={sec}:r=30", "-f", "lavfi", "-i",
                     f"sine=f=440:d={sec}", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"])
        card = os.path.join(d, "end_card_BLACK.mp4")   # a white box on black, like the brand's card
        make(card, ["-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=3:r=30", "-vf",
                    "drawbox=x=240:y=600:w=600:h=300:color=white:t=fill", "-c:v", "libx264", "-pix_fmt", "yuv420p"])
        cid = campaigns.suggest({"name": "Lucia", "platforms": ["instagram"], "rate_per_1k": 1.5}, "pasted")
        db.x("UPDATE campaigns SET min_len=8, max_len=20, stage='active', paused=0 WHERE id=?", cid)
        recipes.set_recipe(cid, card)
        for vid, p in (("v1", short), ("v2", other)):
            db.x("INSERT INTO videos(id, campaign_id, title, local_path, status, created_at) VALUES(?,?,?,?,?,?)",
                 vid, cid, vid, p, "new", "2026-01-01T00:00:00Z")
        c = campaigns.get(cid)
        with mock.patch("clipfactory.check.ready", return_value=True):
            pipeline.stage_analyze(c)
        ms = db.q("SELECT * FROM moments WHERE campaign_id=?", cid)
        self.assertEqual(len(ms), 2)
        self.assertTrue(all(m["category"] == "ai_reveal" and m["score"] < 9 for m in ms))   # review first
        pipeline.stage_download(c, limit=1)
        with mock.patch.object(qa, "check", return_value={"score": 9, "issues": [], "summary": "ok"}):
            pipeline.stage_render(c)
        clip = db.one("SELECT * FROM clips WHERE campaign_id=? AND status='rendered'", cid)
        self.assertIsNotNone(clip, db.q("SELECT error FROM clips"))
        plan = json.loads(clip["plan"])
        self.assertEqual(plan["recipe"], "ai_reveal")
        self.assertEqual(plan["parts"], 2)                          # the 5 s clip got a second one (min 8 s)
        self.assertTrue(8 <= clip["duration_s"] <= 20)
        self.assertEqual(campaigns._extra(campaigns.get(cid))["captions"], "off")

        def luma(t):   # brightness where the card's white box lands (moved down 560 px)
            out = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", clip["final_path"], "-frames:v", "1",
                                  "-vf", "crop=200:100:440:1260,scale=1:1,format=gray", "-f", "rawvideo", "-"],
                                 capture_output=True).stdout
            return out[0] if out else 0
        self.assertLess(luma(1.0), 120)                              # before the end card: the dark footage
        self.assertGreater(luma(clip["duration_s"] - 1.0), 200)      # last seconds: the card's box shows through
        self.assertIn("campaign's format", qa.added_text("ai_reveal", False, plan, ""))

    def test_end_card_moves_off_a_caption_already_in_the_clip(self):
        import numpy as np
        from clipfactory import layout, recipes
        d = self.tmp.name
        clip = os.path.join(d, "clip.mp4")
        make(clip, ["-f", "lavfi", "-i", "color=c=0x404040:s=720x1280:d=6:r=30", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p"])
        card = os.path.join(d, "card.mp4")   # the logo + text block, like the brand's card
        make(card, ["-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=3:r=30", "-vf",
                    "drawbox=x=240:y=440:w=600:h=520:color=white:t=fill", "-c:v", "libx264", "-pix_fmt", "yuv420p"])
        top, bottom = recipes._card_band(card)
        self.assertTrue(400 <= top <= 480 and 920 <= bottom <= 1000, (top, bottom))

        def band(a, b):
            rows = np.zeros(layout.BANDS)
            rows[int(a / 1920 * layout.BANDS):int(b / 1920 * layout.BANDS) + 1] = 1.0
            return rows
        caption, face = band(1380, 1440), band(400, 700)   # the clip's own caption, and her face
        with mock.patch.object(layout, "text_rows", return_value=caption), \
                mock.patch.object(recipes, "_face_rows", return_value=face):
            shift = recipes.card_shift([clip], 3.0, card)
        a, b = top + shift, bottom + shift
        self.assertTrue(b < 1380 or a > 1440, shift)                            # never on the caption
        self.assertTrue(a > 700, shift)                                          # never on her face
        self.assertGreaterEqual(a, recipes.TOP_TEXT)


if __name__ == "__main__":
    unittest.main()
