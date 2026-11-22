"""Edit styles from the viral-clip research: choice, captions, credit, filters, and a real render if FFmpeg exists.

Run:  python -m unittest tests.test_styles
"""
import os
import random
import shutil
import subprocess
import tempfile
import unittest


class StylesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_choice_fits_the_content(self):
        from clipfactory import styles
        rng = random.Random(1)
        talk = {styles.choose("business podcast", "insight", rng=rng) for _ in range(40)}
        self.assertTrue(talk <= {"bold_crop", "word_pop", "black_title", "blur_box"})
        self.assertIn("bold_crop", talk)
        show = {styles.choose("live concert", "funny", rng=rng) for _ in range(40)}
        self.assertIn("meme_top", show)
        wide = {styles.choose("GTA 6 gameplay", "fail", rng=rng) for _ in range(40)}
        self.assertIn("blur_box", wide)
        self.assertEqual(styles.choose("streamer", "", facecam=True), "split")
        for _ in range(20):
            self.assertNotEqual(styles.choose("podcast", "", avoid="bold_crop", rng=rng), "bold_crop")

    def test_every_remake_of_a_clip_gets_a_new_style(self):
        from clipfactory import campaigns, db, pipeline
        db._local.__dict__.clear()
        cid = campaigns.suggest({"name": "Band Live", "platforms": ["instagram"], "rate_per_1k": 2,
                                 "topic": "live concert"}, "pasted")
        m = {"id": 1, "category": "funny"}
        seen = [pipeline.pick_style(campaigns.get(cid), m, "blur", None, {"id": 9}) for _ in range(5)]
        self.assertEqual(len(set(seen)), 5, seen)                  # 5 remakes, 5 different designs

    def test_short_credit_and_clean_title(self):
        from clipfactory import styles
        self.assertEqual(styles.short_credit("Credit: @mby on Instagram & YouTube, @mbyclips on TikTok"), "@mby")
        self.assertEqual(styles.short_credit("no handle here"), "")
        self.assertEqual(styles.on_screen_title("He did NOT expect this 😭💀"), "He did NOT expect this")

    def test_ass_has_captions_title_and_safe_positions(self):
        from clipfactory import styles
        words = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.3} for i, w in enumerate("the crowd went absolutely insane".split())]
        for st in styles.STYLE_NAMES:
            ass = styles.build_ass(st, words, "Wait for it 😭", 3.0, "Credit: @mby", "#3CFF5A")
            self.assertIn("&H005AFF3C&", ass)                          # green accent in BGR
            self.assertNotIn("😭", ass)
            ys = [int(y) for y in __import__("re").findall(r"\\pos\(540,(\d+)\)", ass)]
            self.assertTrue(ys and max(ys) <= 1450, (st, ys))          # nothing under the platform buttons
            self.assertIn("@mby", ass)
        self.assertIn("WAIT FOR IT", styles.build_ass("meme_top", words, "Wait for it", 3.0))

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs FFmpeg")
    def test_real_render_of_every_style(self):
        from clipfactory import render
        src = os.path.join(self.tmp.name, "src.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=2",
                        "-f", "lavfi", "-i", "sine=d=2", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", src], check=True)
        words = [{"w": "hello", "s": 0.2, "e": 0.6}, {"w": "there", "s": 0.7, "e": 1.1}]
        for st in ("bold_crop", "word_pop", "blur_box", "black_title", "meme_top"):
            out = os.path.join(self.tmp.name, f"{st}_final.mp4")
            res = render.render(src, out, words, "Wait for it", style=st, credit="@mby")
            self.assertGreater(os.path.getsize(res["path"]), 10000, st)
            info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                   "stream=width,height", "-of", "csv=p=0", out], capture_output=True, text=True).stdout
            self.assertEqual(info.strip(), "1080,1920", st)


if __name__ == "__main__":
    unittest.main()
