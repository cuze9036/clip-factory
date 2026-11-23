"""Selftest lines for Instagram / Facebook keys. No network.

Run:  python -m unittest tests.test_meta_selftest
"""
import os
import tempfile
import unittest
from unittest import mock

KEYS = {"IG_MAIN_USER_ID": "1784", "IG_MAIN_TOKEN": "EAAsecret", "FB_MAIN_PAGE_ID": "1029", "FB_MAIN_PAGE_TOKEN": "EAAsecret"}


def _resp(status, body):
    return mock.Mock(status_code=status, json=lambda: body, headers={"content-type": "application/json"})


class MetaSelftestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name

    def tearDown(self):
        for k in KEYS:
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_not_set_is_skip(self):
        from clipfactory import selftest
        line = selftest._check("Instagram posting", selftest._meta("instagram"))
        self.assertTrue(line.startswith("SKIP"), line)
        self.assertIn("you post Instagram clips yourself", line)

    def test_ok_shows_account_and_keeps_token_out_of_url(self):
        from clipfactory import selftest
        os.environ.update(KEYS)
        with mock.patch("requests.get", return_value=_resp(200, {"username": "clipsacct", "id": "1784"})) as g:
            line = selftest._check("Instagram posting", selftest._meta("instagram"))
        self.assertTrue(line.startswith("OK"), line)
        self.assertIn("@clipsacct", line)
        args, kwargs = g.call_args
        self.assertNotIn("EAAsecret", args[0] + str(kwargs.get("params")))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer EAAsecret")
        with mock.patch("requests.get", return_value=_resp(200, {"name": "Clips Page", "id": "1029"})):
            self.assertIn("Clips Page", selftest._check("Facebook posting", selftest._meta("facebook")))

    def test_refused_key_and_network_error_never_show_the_token(self):
        from clipfactory import selftest
        os.environ.update(KEYS)
        bad = _resp(400, {"error": {"message": "Error validating access token: Session has expired"}})
        with mock.patch("requests.get", return_value=bad):
            line = selftest._check("Facebook posting", selftest._meta("facebook"))
        self.assertTrue(line.startswith("FAIL") and "expired" in line, line)
        with mock.patch("requests.get", side_effect=OSError("https://graph.facebook.com/x?access_token=EAAsecret")):
            line = selftest._check("Facebook posting", selftest._meta("facebook"))
        self.assertNotIn("EAAsecret", line)


    def test_instagram_upload_keeps_token_in_headers(self):
        import json
        from clipfactory import db, publish
        os.environ.update(KEYS)
        db._local.__dict__.clear()
        video = os.path.join(self.tmp.name, "clip.mp4")
        with open(video, "wb") as f:
            f.write(b"0" * 100)
        answers = iter([{"id": "c1"}, {"success": True}, {"status_code": "FINISHED"}, {"id": "m1"},
                        {"permalink": "https://www.instagram.com/reel/abc/"}])
        calls = []

        def fake(method, url, headers=None, **kw):
            calls.append((method, url, headers, kw))
            return mock.Mock(status_code=200, json=lambda: next(answers))

        clip = {"title": "WAIT FOR IT", "description": "Big moment.", "hashtags": json.dumps(["#music"]),
                "final_path": video}
        with mock.patch("requests.request", fake):
            mid, link = publish.instagram_upload({"account": "main", "campaign_id": "none"}, clip)
        self.assertEqual((mid, link), ("m1", "https://www.instagram.com/reel/abc/"))
        for method, url, headers, kw in calls:
            self.assertNotIn("EAAsecret", url + str(kw.get("params")) + str(kw.get("data") if isinstance(kw.get("data"), dict) else ""))
            self.assertIn("EAAsecret", headers["Authorization"])
        self.assertTrue(calls[1][2]["Authorization"].startswith("OAuth "))   # the upload host wants "OAuth"

    def test_paste_slips_in_keys_are_cleaned(self):
        from clipfactory import publish, selftest
        os.environ.update({"IG_MAIN_USER_ID": "a17841453437546398", "IG_MAIN_TOKEN": "aEAAsecret ",
                           "FB_MAIN_PAGE_ID": " a1447638601766865", "FB_MAIN_PAGE_TOKEN": "EAAsecret"})
        self.assertEqual(publish.meta_keys("instagram"), ("17841453437546398", "EAAsecret"))
        self.assertEqual(publish.meta_keys("facebook"), ("1447638601766865", "EAAsecret"))
        with mock.patch("requests.get", return_value=_resp(200, {"username": "clipsacct"})) as g:
            self.assertTrue(selftest._check("Instagram posting", selftest._meta("instagram")).startswith("OK"))
        self.assertTrue(g.call_args[0][0].endswith("/17841453437546398"))
        self.assertEqual(g.call_args[1]["headers"]["Authorization"], "Bearer EAAsecret")

    def test_network_error_message_has_no_token(self):
        import requests
        from clipfactory import publish
        with mock.patch("requests.request", side_effect=requests.ConnectionError("https://x?access_token=EAAsecret")):
            with self.assertRaises(RuntimeError) as e:
                publish._g("GET", "https://graph.facebook.com/v21.0/1", "EAAsecret")
        self.assertNotIn("EAAsecret", str(e.exception))


if __name__ == "__main__":
    unittest.main()
