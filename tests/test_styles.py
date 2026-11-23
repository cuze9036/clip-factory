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
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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

    def test_gpu_failure_pauses_for_6_hours_not_forever(self):
        import datetime as dt
        from unittest import mock
        from clipfactory import db, render
        db._local.__dict__.clear()
        enc = mock.Mock(stdout="V..... h264_nvenc")
        with mock.patch("subprocess.run", return_value=enc):
            self.assertTrue(render.use_nvenc({"encoder": "auto"}))
            db.kv_set("nvenc_pause_until", render.__dict__.get("iso", __import__("clipfactory.util", fromlist=["iso"]).iso)(
                __import__("clipfactory.util", fromlist=["utcnow"]).utcnow() + dt.timedelta(hours=6)))
            self.assertFalse(render.use_nvenc({"encoder": "auto"}))
            db.kv_set("nvenc_pause_until", "2020-01-01T00:00:00Z")
            self.assertTrue(render.use_nvenc({"encoder": "auto"}))

    def test_a_bad_filter_does_not_switch_the_gpu_off(self):
        from unittest import mock
        from clipfactory import db, render
        db._local.__dict__.clear()
        with mock.patch.object(render, "use_nvenc", return_value=True),                 mock.patch.object(render, "run", side_effect=RuntimeError("crop: Invalid too big")):
            with self.assertRaises(RuntimeError):
                render._encode(["ffmpeg"], "out.mp4", {})
        self.assertIsNone(db.kv_get("nvenc_pause_until"))   # the CPU failed too: not the GPU's fault
        calls = []
        with mock.patch.object(render, "use_nvenc", return_value=True),                 mock.patch.object(render, "run", side_effect=lambda cmd: calls.append(cmd) or (
                    (_ for _ in ()).throw(RuntimeError("nvenc: no device")) if "h264_nvenc" in cmd else None)):
            render._encode(["ffmpeg"], "out.mp4", {})
        self.assertIn("libx264", calls[-1])
        self.assertIsNotNone(db.kv_get("nvenc_pause_until"))   # the CPU managed: now the GPU rests 6 hours

    def test_vertical_footage_with_its_own_captions_keeps_the_whole_frame(self):
        from clipfactory import campaigns, db, pipeline
        db._local.__dict__.clear()
        cid = campaigns.suggest({"name": "Kitsch", "platforms": ["instagram"], "rate_per_1k": 2,
                                 "topic": "hair care gifts"}, "pasted")
        m = {"id": 3, "category": "funny"}
        for k in range(3):
            self.assertEqual(pipeline.pick_style(campaigns.get(cid), m, "blur", None, {"id": k}, captions=False,
                                                 vertical=True), "bold_crop")
        self.assertNotIn(pipeline.pick_style(campaigns.get(cid), m, "blur", None, {"id": 5}, captions=False),
                         ("bold_crop", "word_pop", "split"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs FFmpeg")
    def test_every_style_fits_tall_and_square_footage(self):
        from clipfactory import styles
        for w, h in ((1080, 1920), (1440, 1080), (1920, 1080)):
            for style in styles.STYLE_NAMES:
                vf = styles.video_filter(style, {"width": w, "height": h, "fps": 30, "duration": 1.0},
                                         facecam=(0.0, 0.6, 0.3, 0.3) if style == "split" else None, duration=1.0)
                r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r=30:d=1",
                                    "-filter_complex", vf, "-map", "[v0]", "-f", "null", "-"],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, f"{style} on {w}x{h}: {r.stderr[-300:]}")

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
