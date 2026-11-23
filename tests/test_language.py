"""Other-language footage: allowed unless the campaign wants English only; captions are translated to English.

Run:  python -m unittest tests.test_language
"""
import os
import tempfile
import unittest
from unittest import mock


class LanguageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def camp(self, rules=(), guide=None):
        c = {"id": "c1", "rules": list(rules), "audience": "", "notes": "{}"}
        if guide:
            import json
            c["notes"] = json.dumps({"guide": guide})
        return c

    def test_any_language_by_default(self):
        from clipfactory import analyst
        self.assertFalse(analyst.english_only(self.camp(["Tag the creator", "No reuploads"])))

    def test_campaign_rule_english_only(self):
        from clipfactory import analyst
        self.assertTrue(analyst.english_only(self.camp(["Content must be in English"])))
        self.assertTrue(analyst.english_only(self.camp(["English-speaking audience only"])))

    def test_guide_says_english_only(self):
        from clipfactory import analyst
        c = self.camp(guide={"do": ["Use English only clips"], "avoid": []})
        with mock.patch("clipfactory.campaigns._extra", return_value={"guide": {"do": ["Use English only clips"]}}):
            self.assertTrue(analyst.english_only(c))

    def test_translate_options(self):
        from clipfactory import transcribe
        self.assertEqual(transcribe._opts(False), {"language": "en"})
        self.assertEqual(transcribe._opts(True), {"task": "translate"})

    def test_translate_uses_multilingual_model(self):
        from clipfactory import transcribe

        class Seg:
            start, end, text = 0.0, 1.0, " Hello there"
        english, multi = mock.Mock(), mock.Mock()
        english.transcribe.return_value = ([Seg()], None)
        multi.transcribe.return_value = ([Seg()], None)
        with mock.patch.object(transcribe, "_run", side_effect=lambda go: go(english)), \
                mock.patch.object(transcribe, "_get_multi", return_value=multi):
            transcribe.segments("a.wav", translate=True)
            transcribe.segments("a.wav")
        self.assertEqual(multi.transcribe.call_args.kwargs.get("task"), "translate")
        self.assertEqual(english.transcribe.call_args.kwargs.get("language"), "en")


if __name__ == "__main__":
    unittest.main()
