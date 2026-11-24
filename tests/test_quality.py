"""Edits that don't look cheap: our text never on text already in the picture, a webcam becomes the split layout,
captions read naturally, and the AI check fixes a failing clip by itself. No network.

Run:  python -m unittest tests.test_quality
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import numpy as np


def _frame_with_text(y_px, text="WATCH THIS NOW"):
    import cv2
    f = np.full((960, 540, 3), (70, 120, 150), np.uint8)
    (w, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, 1.2, 3)
    cv2.putText(f, text, ((540 - w) // 2, y_px), cv2.FONT_HERSHEY_DUPLEX, 1.2, (255, 255, 255), 3)
    return f


class LayoutTest(unittest.TestCase):
    def test_text_in_the_picture_is_found_where_it_is(self):
        from clipfactory import layout
        rows = layout.text_rows([_frame_with_text(700)] * 4)   # 700 of 960 = 1400 of 1920
        taken = [i * layout.H / layout.BANDS for i, v in enumerate(rows) if v >= layout.TAKEN]
        self.assertTrue(taken and all(1300 <= y <= 1450 for y in taken), taken)
        self.assertEqual(layout.text_rows([np.full((960, 540, 3), 90, np.uint8)] * 4).max(), 0)

    def test_our_captions_and_handle_move_off_that_text(self):
        from clipfactory import layout, styles
        rows = np.zeros(layout.BANDS)
        rows[int(1240 / 48) - 1:int(1240 / 48) + 2] = 1.0            # their subtitles right where ours would go
        rows[int(1390 / 48)] = 1.0
        pos = layout.place(styles.LOOK["bold_crop"], rows, captions=True, hook=True)
        self.assertLess(layout.busy(rows, pos["cap_y"], layout.HALF["cap"]), layout.TAKEN)
        self.assertLess(layout.busy(rows, pos["credit_y"], layout.HALF["credit"]), layout.TAKEN)
        self.assertGreater(abs(pos["credit_y"] - pos["cap_y"]), layout.HALF["cap"])
        free = layout.place(styles.LOOK["bold_crop"], np.zeros(layout.BANDS), captions=True)
        self.assertEqual((free["cap_y"], free["credit_y"]), (1240, 1390))   # nothing in the way: usual spots

    def test_the_ass_file_uses_the_chosen_spots(self):
        from clipfactory import styles
        words = [{"w": "hello", "s": 0.1, "e": 0.4}, {"w": "there", "s": 0.5, "e": 0.8}]
        ass = styles.build_ass("bold_crop", words, "Hook", 2.0, "@me", plan={"hook": {"text": "Big hook", "until": 1}},
                               positions={"cap_y": 800, "credit_y": 1790, "hook_y": 480})
        self.assertIn("\pos(540,1790)", ass)
        self.assertIn("\pos(540,480)", ass)
        self.assertIn(",800)", ass)


class CaptionLinesTest(unittest.TestCase):
    def test_lines_end_naturally(self):
        from clipfactory import styles
        said = "nice okay, done I am the one who is going to win".split()
        words = [{"w": w, "s": i * 0.3, "e": i * 0.3 + 0.25} for i, w in enumerate(said)]
        lines = [" ".join(w["w"] for w in g) for g in styles._groups(words, 3)]
        self.assertEqual(lines[0], "nice okay,")                     # a comma ends the line
        self.assertTrue(all(ln.split()[-1].lower() not in styles.WEAK for ln in lines[:-1]), lines)


class FacecamTest(unittest.TestCase):
    def test_webcam_footage_never_gets_a_face_following_crop(self):
        import random
        from clipfactory import styles
        rng = random.Random(3)
        self.assertEqual(styles.choose("GTA 6 gameplay", "fail", facecam=True), "split")
        for _ in range(30):
            s = styles.choose("GTA 6 gameplay", "fail", facecam=True, avoid=["split"], rng=rng)
            self.assertNotIn(s, ("bold_crop", "word_pop"))

    def test_game_half_is_cut_beside_the_webcam(self):
        from clipfactory import styles
        vf = styles.video_filter("split", {"width": 1920, "height": 1080, "fps": 30}, facecam=[0.7, 0.03, 0.25, 0.44],
                                 duration=5.0)
        self.assertIn("max(0,0.7*iw-ow)", vf)                        # webcam on the right: the game crop stays left


class QaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_verdicts(self):
        from clipfactory import qa
        bad = qa._validate({"score": "4", "fix": "captions_off", "issues": [
            {"type": "double_text", "detail": "ours on theirs", "severity": "high"}, {"type": "weird"}]})
        self.assertFalse(qa.passed(bad))
        self.assertEqual(bad["issues"][1]["type"], "other")
        self.assertTrue(qa.passed(qa._validate({"score": 8, "issues": [{"type": "boring", "severity": "low"}]})))
        self.assertFalse(qa.passed(qa._validate({"score": 9, "issues": [{"type": "rule_break", "severity": "high"}]})))
        self.assertTrue(qa.passed(None))                               # no video AI: nothing blocks the clip
        self.assertIn("AI check 4/10", qa.text(bad))

    def test_a_failing_clip_fixes_itself_then_goes_to_review(self):
        from clipfactory import campaigns, db, pipeline, qa
        cid = campaigns.suggest({"name": "Kitsch", "platforms": ["instagram"], "rate_per_1k": 2}, "pasted")
        db.x("INSERT INTO moments(id, video_id, campaign_id, start_s, end_s, score, created_at) VALUES(5,'v',?,0,12,9,'t')",
             cid)
        db.x("INSERT INTO clips(id, moment_id, campaign_id, raw_path, status, created_at) VALUES(7,5,?,'r.mp4','rendered','t')",
             cid)
        bad = {"score": 3.0, "issues": [{"type": "double_text", "detail": "x", "severity": "high"}],
               "fix": "captions_off", "summary": ""}
        clip = dict(db.one("SELECT * FROM clips WHERE id=7"), style="bold_crop")
        with mock.patch.object(qa, "check", return_value=bad):
            for _ in range(3):
                pipeline.check_clip(campaigns.get(cid), clip, {"path": "f.mp4", "duration": 12}, True)
        self.assertTrue(db.kv_get("captions_off:5"))                   # next render leaves our captions out
        self.assertEqual(qa.tries(5), 2)                               # then it stops trying
        self.assertEqual(json.loads(db.one("SELECT qa FROM clips WHERE id=7")["qa"])["score"], 3.0)
        self.assertFalse(qa.passed(qa.get(db.one("SELECT * FROM clips WHERE id=7"))))   # never auto-approved


class StreamsTest(unittest.TestCase):
    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "needs FFmpeg")
    def test_picture_and_sound_end_together(self):
        import subprocess
        from clipfactory import download
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            p = os.path.join(d, "m_raw.mp4")
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2",
                            "-f", "lavfi", "-i", "sine=f=440:d=5", "-c:v", "libx264", "-c:a", "aac", p], check=True)
            self.assertTrue(download.even_streams(p))
            n = download.stream_lengths(p)
            self.assertLess(abs(n["video"] - n["audio"]), 0.3, n)
            self.assertFalse(download.even_streams(p))                  # already even: left alone


class SplitStaysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_webcam_footage_stays_split_unless_another_look_is_asked(self):
        from clipfactory import campaigns, db, pipeline
        cid = campaigns.suggest({"name": "GTA", "platforms": ["youtube"], "topic": "GTA 6 gameplay"}, "pasted")
        c, m, cam = campaigns.get(cid), {"id": 2, "category": "fail"}, [0.7, 0.03, 0.25, 0.44]
        for k in range(3):   # a re-cut of the same moment keeps the split layout
            self.assertEqual(pipeline.pick_style(c, m, "facecam_split", cam, {"id": k}), "split")
        db.kv_set("restyle:9", "split")
        self.assertNotIn(pipeline.pick_style(c, m, "facecam_split", cam, {"id": 9}), ("split", "bold_crop", "word_pop"))


class CampaignLanguageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_arabic_campaign_gets_arabic_titles_and_newest_guide_first(self):
        from clipfactory import campaigns, guide, llm, metadata
        cid = campaigns.suggest({"name": "CoD MW4", "platforms": ["instagram"], "rate_per_1k": 1}, "pasted")
        old = {"do": [f"old rule {i}" for i in range(15)], "avoid": [], "hashtags": [], "credit": "",
               "clip_length": None, "post_language": ""}
        new = dict(old, do=["Use split screen for the podcast"], post_language="arabic")
        with mock.patch.object(llm, "complete_json", side_effect=[(old, "x"), (guide._validate(new), "x")]):
            guide.add(cid, "old brief", expand_docs=False)
            guide.add(cid, "الكابشن يجب أن يكون باللغة العربية", expand_docs=False)
        c = campaigns.get(cid)
        self.assertEqual(guide.get(c)["do"][0], "Use split screen for the podcast")   # newest first, not cut off
        self.assertEqual(metadata.post_language(c), "Arabic")
        arabic = {"titles": ["لحظة جنونية في كود"], "description": "شاهد النهاية", "hashtags": ["#كود", "#MW4"]}
        seen = {}

        def fake(task, prompt, validate):
            seen["prompt"] = prompt
            return validate(arabic), "x"
        with mock.patch.object(llm, "complete_json", side_effect=fake):
            meta = metadata.write("", {"category": "funny", "hook": "h", "why": "w"}, c)
        self.assertIn("native Arabic", seen["prompt"])
        self.assertEqual(meta["titles"][0], "لحظة جنونية في كود")
        self.assertIn("#كود", meta["hashtags"])
