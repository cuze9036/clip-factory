"""AI router: free models first for text work, Gemini for watching videos, usage counted. No network.

Run:  python -m unittest tests.test_llm_router
"""
import os
import tempfile
import unittest
from unittest import mock

KEYS = {"GEMINI_API_KEY": "g", "FREE_LLM_BASE_URL": "http://free.example/v1", "FREE_LLM_API_KEY": "f",
        "FREE_LLM_MODEL": "auto:default"}


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        os.environ.update(KEYS)
        from clipfactory import db, llm
        db._local.__dict__.clear()
        llm._cooldown.clear()

    def tearDown(self):
        for k in KEYS:
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_text_work_goes_to_the_free_model_first(self):
        from clipfactory import llm
        with mock.patch.object(llm, "_openai", return_value='{"ok": 1}') as free, \
                mock.patch.object(llm, "_gemini", return_value='{"ok": 2}') as gem:
            data, who = llm.complete_json("metadata", "prompt A", lambda d: d)
        self.assertEqual((data, who), ({"ok": 1}, "free-llm"))
        free.assert_called_once()
        gem.assert_not_called()

    def test_gemini_is_the_fallback_for_text(self):
        from clipfactory import llm
        with mock.patch.object(llm, "_openai", side_effect=RuntimeError("free model down")), \
                mock.patch.object(llm, "_gemini", return_value='{"ok": 2}'):
            data, who = llm.complete_json("metadata", "prompt B", lambda d: d)
        self.assertEqual(who, "gemini")
        out = llm.usage()
        self.assertIn("**gemini**: 1 answers, 0 failed · metadata 1", out)
        self.assertIn("**free-llm**: 0 answers, 1 failed", out)

    def test_watching_a_video_uses_gemini_only(self):
        from clipfactory import llm
        with mock.patch.object(llm, "_openai") as free, \
                mock.patch.object(llm, "_gemini", return_value='{"moments": []}'):
            _, who = llm.complete_json("moments_video", "prompt C", lambda d: d, video={"url": "https://youtu.be/x"})
        self.assertEqual(who, "gemini")
        free.assert_not_called()

    def test_ai_command(self):
        from clipfactory import commands, llm
        with mock.patch.object(llm, "_openai", return_value='{"ok": 1}'):
            llm.complete_json("campaign_guide", "prompt D", lambda d: d)
        self.assertIn("free-llm**: 1 answers", commands.handle("/ai"))


if __name__ == "__main__":
    unittest.main()
