"""Edit effects: the automatic plan, cleaning an AI plan, jump cuts, face-follow crop, and a real render.

Run:  python -m unittest tests.test_effects
"""
import os
import shutil
import subprocess
import tempfile
import unittest


def _words(text, pause_at=None, pause=1.4):
    out, t = [], 0.3
    for i, w in enumerate(text.split()):
        if i == pause_at:
            t += pause
        out.append({"w": w, "s": round(t, 2), "e": round(t + 0.26, 2)})
        t += 0.33
    return out


class EffectsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_auto_plan_finds_pauses_strong_words_and_emojis(self):
        from clipfactory import effects
        w = _words("so I walked on stage and bro the crowd went absolutely insane! haha", pause_at=8)
        p = effects.auto_plan(w, 6.0, "He did NOT expect this", "bold_crop")
        self.assertEqual(len(p["cuts"]), 1)
        self.assertTrue(p["zooms"] and len(p["zooms"]) <= 3)
        self.assertIn(11, p["emphasis"])                               # "insane!"
        self.assertEqual(p["emojis"][0]["emoji"], "🤯")
        self.assertEqual(p["hook"]["text"], "He did NOT expect this")
        self.assertNotIn("hook", effects.auto_plan(w, 6.0, "x", "meme_top"))   # meme has its own title

    def test_clean_makes_a_bad_plan_safe(self):
        from clipfactory import effects
        bad = {"cuts": [[5, 1], [0, 5.9]], "zooms": [{"t": 1, "scale": 9}, {"t": 99}], "flashes": [1, "x"],
               "sfx": [{"t": 1, "kind": "explosion"}, {"t": 1, "kind": "pop"}], "emphasis": [2, "x", -1],
               "hook": {"text": "WAIT", "until": 30}}
        p = effects.clean(bad, 6.0)
        self.assertEqual(p["cuts"], [])                                # too much cut: dropped
        self.assertEqual(p["zooms"], [{"t": 1.0, "scale": 1.4, "dur": 0.6}])
        self.assertEqual(p["flashes"], [1.0])
        self.assertEqual(p["sfx"], [{"t": 1.0, "kind": "pop"}])
        self.assertEqual(p["emphasis"], [2])
        self.assertEqual(p["hook"]["until"], 4.0)

    def test_cuts_move_words_and_effects(self):
        from clipfactory import effects
        spans = effects.keep_spans([[2.0, 3.0]], 6.0)
        self.assertEqual(spans, [[0.0, 2.0], [3.0, 6.0]])
        self.assertEqual(effects.remap(3.5, spans), 2.5)
        self.assertIsNone(effects.remap(2.5, spans))
        words = [{"w": "a", "s": 1.0, "e": 1.2}, {"w": "b", "s": 2.5, "e": 2.7}, {"w": "c", "s": 4.0, "e": 4.2}]
        nw, p = effects.apply_cuts(words, {"zooms": [{"t": 4.0, "scale": 1.2, "dur": 0.5}], "emphasis": [2]}, spans)
        self.assertEqual([x["w"] for x in nw], ["a", "c"])
        self.assertEqual(nw[1]["s"], 3.0)
        self.assertEqual(p["zooms"][0]["t"], 3.0)
        self.assertEqual(p["emphasis"], [1])                            # "c" is now word 1

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs FFmpeg")
    def test_face_follow_expression_runs_in_ffmpeg(self):
        from clipfactory import effects
        x = effects.x_expr([(0.0, 0.3), (0.5, 0.7), (1.0, 0.5)], "ow")
        r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=10:d=1",
                            "-vf", f"crop=w='min(iw,ih*9/16)':h='min(ih,iw*16/9)':x='{x}':y=0", "-f", "null", "-"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs FFmpeg")
    def test_render_with_effects_cuts_the_pause(self):
        from clipfactory import render, styles
        src = os.path.join(self.tmp.name, "src.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6",
                        "-f", "lavfi", "-i", "sine=d=6", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", src], check=True)
        w = _words("so I walked on stage and bro the crowd went absolutely insane! haha", pause_at=8)
        out = os.path.join(self.tmp.name, "a_final.mp4")
        res = render.render(src, out, w, "He did NOT expect this", style="bold_crop", credit="@x",
                            accent=styles.accent_for("comedy"))
        self.assertLess(res["duration"], 5.2)                         # the 1.4 s pause was cut
        self.assertTrue(res["plan"]["zooms"])
        streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", out],
                                 capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(streams), ["audio", "video"])


    @unittest.skipUnless(shutil.which("ffmpeg"), "needs FFmpeg")
    def test_footage_with_its_own_captions_gets_none_of_ours(self):
        from clipfactory import campaigns, commands, db, effects, pipeline, styles
        plain = os.path.join(self.tmp.name, "plain.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=10:d=3",
                        "-c:v", "libx264", "-preset", "ultrafast", plain], check=True)
        boxed = os.path.join(self.tmp.name, "boxed.mp4")
        # drawtext without a font file crashes ffmpeg on Windows (no fontconfig), so hand it one when there is one
        font = next((f for f in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf",
                                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf") if os.path.exists(f)), None)
        fontfile = ""
        if font:
            shutil.copy(font, os.path.join(self.tmp.name, "f.ttf"))
            fontfile = "fontfile=f.ttf:"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", plain, "-vf",
                        f"drawtext={fontfile}text='THE CROWD WENT WILD':fontcolor=white:fontsize=72:borderw=6:"
                        "bordercolor=black:x=(w-tw)/2:y=h*0.72", "-c:v", "libx264", "-preset", "ultrafast", boxed],
                       check=True, cwd=self.tmp.name)
        self.assertTrue(effects.has_burned_captions(boxed))
        self.assertFalse(effects.has_burned_captions(plain))
        db._local.__dict__.clear()
        cid = campaigns.suggest({"name": "Band Live", "platforms": ["instagram"], "rate_per_1k": 2}, "pasted")
        c = campaigns.get(cid)
        self.assertFalse(pipeline.want_captions(c, {"id": "v1", "title": "t"}, boxed))
        style = pipeline.pick_style(c, {"id": 1, "category": "funny"}, "blur", None, {"id": 1}, captions=False)
        self.assertNotIn(style, ("bold_crop", "word_pop", "split"))   # the whole frame stays visible
        self.assertIn("never added", commands.set_captions(cid, "off"))
        self.assertFalse(pipeline.want_captions(campaigns.get(cid), {"id": "v2", "title": "t"}, plain))
        ass = styles.build_ass("blur_box", [{"w": "hello", "s": 0.1, "e": 0.4}], "Hook", 2.0, captions=False)
        self.assertNotIn("HELLO", ass)
        self.assertIn("Hook", ass)                                        # the title box stays


if __name__ == "__main__":
    unittest.main()
