"""Whole-video analysis -> clip plan -> moments with edit plans; Gemini file upload; Claude budget. No network.

Run:  python -m unittest tests.test_analyst
"""
import json
import os
import tempfile
import unittest
from unittest import mock

PART = {"summary": "A band plays live; the singer falls and the crowd goes wild.", "language": "en",
        "on_screen_text": False, "logos": "",
        "scenes": [{"start": "00:10", "end": "00:40", "shows": "singer on stage", "who": "singer", "energy": 6,
                    "usable": "great", "note": ""}],
        "highlights": [{"start": "02:05", "end": "02:30", "what": "singer slips, crowd screams", "why": "shock",
                        "energy": 9}],
        "skip": [{"start": "05:00", "end": "06:00", "why": "sponsor read"}]}
PLAN = {"thinking": "Live concert; the fall is the clear peak.",
        "clips": [{"start": "02:03", "end": "02:28", "score": 9, "category": "shock", "hook": "HE DID NOT SEE IT",
                   "why": "huge reaction",
                   "edit": {"zooms": [{"t": 3.1, "scale": 1.2}], "flashes": [3.1],
                            "emojis": [{"t": 4.0, "emoji": "😳"}], "emphasis_words": ["insane"], "cuts": []}},
                  {"start": "02:10", "end": "02:30", "score": 8, "category": "reaction", "hook": "overlap",
                   "why": "x", "edit": {}},
                  {"start": "08:00", "end": "08:20", "score": 4, "category": "funny", "hook": "weak", "why": "x",
                   "edit": {}}]}


class AnalystTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db, llm
        db._local.__dict__.clear()
        llm._cooldown.clear()

    def tearDown(self):
        for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "FREE_LLM_BASE_URL", "FREE_LLM_API_KEY", "FREE_LLM_MODEL"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def _setup(self):
        from clipfactory import campaigns, db
        cid = campaigns.suggest({"name": "Band Live", "platforms": ["instagram"], "rate_per_1k": 2,
                                 "topic": "live concert"}, "pasted")
        campaigns.activate(cid)
        path = os.path.join(self.tmp.name, "show.mp4")
        open(path, "wb").close()
        db.x("INSERT INTO videos(id, campaign_id, title, local_path, duration_s, is_stream, status) VALUES(?,?,?,?,?,?,?)",
             "v1", cid, "Show 1", path, 1500, 0, "new")
        return cid, campaigns.get(cid), db.one("SELECT * FROM videos WHERE id='v1'")

    def test_watch_plan_and_moments(self):
        from clipfactory import analyst, db, llm
        cid, c, v = self._setup()
        calls = []

        def fake(task, prompt, validate, video=None):
            calls.append((task, video))
            return (validate(PART) if task == "video_watch" else validate(PLAN)), "fake-ai"

        with mock.patch.object(llm, "complete_json", side_effect=fake), \
                mock.patch.object(llm, "can_watch_video", return_value=True), \
                mock.patch.object(analyst, "_transcript", return_value=("[02:05] that was insane", [])):
            kept = analyst.analyze(v, c)
        watches = [x for x in calls if x[0] == "video_watch"]
        self.assertEqual(len(watches), 2)                                   # 25 min video, 15 min parts
        self.assertEqual(watches[0][1]["path"], v["local_path"])          # local file goes to Gemini
        self.assertEqual(len(kept), 1)                                     # overlap and weak clip dropped
        m = db.one("SELECT * FROM moments")
        self.assertEqual((m["start_s"], m["end_s"], m["hook"]), (123.0, 148.0, "HE DID NOT SEE IT"))
        self.assertEqual(json.loads(m["plan"])["zooms"][0]["t"], 3.1)
        rep = json.loads(db.one("SELECT report FROM videos WHERE id='v1'")["report"])
        self.assertEqual(len(rep["highlights"]), 2)
        msg = db.q("SELECT text FROM outbox")[-1]["text"]
        self.assertIn("🧠 **Video understood**", msg)
        self.assertIn("1 clip(s) planned", msg)
        self.assertIn("Live concert; the fall is the clear peak.", msg)

    def test_director_plan_becomes_an_effects_plan(self):
        from clipfactory import pipeline
        m = {"plan": json.dumps({"zooms": [{"t": 1.0, "scale": 1.2, "dur": 0.6}], "emphasis_words": ["insane"]}),
             "hook": "WAIT FOR IT"}
        words = [{"w": "that", "s": 0.1, "e": 0.3}, {"w": "was", "s": 0.4, "e": 0.6}, {"w": "INSANE!", "s": 0.7, "e": 1.0}]
        p = pipeline.director_plan(m, words)
        self.assertEqual(p["emphasis"], [2])
        self.assertEqual(p["hook"]["text"], "WAIT FOR IT")
        self.assertIsNone(pipeline.director_plan({"plan": None}, words))

    def test_falls_back_to_quick_finder(self):
        from clipfactory import analyst, moments, pipeline
        cid, c, v = self._setup()
        with mock.patch.object(analyst, "analyze", side_effect=RuntimeError("Gemini busy")), \
                mock.patch.object(moments, "analyze", return_value=[]) as quick:
            pipeline.stage_analyze(c)
        quick.assert_called_once()

    def test_gemini_upload_waits_until_active(self):
        from clipfactory import llm

        class F:
            def __init__(self, state):
                self.state, self.name, self.uri, self.mime_type = state, "files/1", "https://g/files/1", "video/mp4"

        client = mock.Mock()
        client.files.upload.return_value = F("PROCESSING")
        client.files.get.return_value = F("ACTIVE")
        path = os.path.join(self.tmp.name, "a.mp4")
        with open(path, "wb") as f:
            f.write(b"x")
        with mock.patch("time.sleep"):
            up = llm.upload_video(client, path)
            again = llm.upload_video(client, path)
        self.assertEqual(up["uri"], "https://g/files/1")
        self.assertEqual(client.files.upload.call_count, 1)               # reused, not uploaded twice
        self.assertEqual(again["uri"], up["uri"])

    def test_claude_thinks_within_budget_then_free_ai_takes_over(self):
        from clipfactory import llm
        os.environ.update(ANTHROPIC_API_KEY="sk-test", FREE_LLM_BASE_URL="http://free.example/v1",
                          FREE_LLM_API_KEY="f", FREE_LLM_MODEL="auto:default")
        resp = mock.Mock(stop_reason="end_turn", content=[mock.Mock(type="text", text='{"ok": 1}')],
                         usage=mock.Mock(input_tokens=50000, output_tokens=5000, cache_creation_input_tokens=0,
                                         cache_read_input_tokens=0))
        fake_client = mock.Mock()
        fake_client.beta.messages.create.return_value = resp
        with mock.patch("anthropic.Anthropic", return_value=fake_client), \
                mock.patch.object(llm, "_openai", return_value='{"ok": 2}') as free:
            self.assertEqual(llm.complete_json("video_plan", "plan A", lambda d: d)[1], "claude")
            self.assertAlmostEqual(llm.claude_spent_today(), 0.30, places=2)   # 50k in + 5k out on Opus
            self.assertEqual(llm.complete_json("metadata", "titles", lambda d: d)[1], "free-llm")  # not a thinking task
            llm.complete_json("video_plan", "plan B", lambda d: d)        # $0.60 > $0.50 budget after this
            self.assertEqual(llm.complete_json("video_plan", "plan C", lambda d: d)[1], "free-llm")
        self.assertEqual(fake_client.beta.messages.create.call_count, 2)
        self.assertGreaterEqual(free.call_count, 2)


if __name__ == "__main__":
    unittest.main()
