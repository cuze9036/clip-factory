"""Instagram + Facebook in one paste: the token is checked, made long-lived, the Page + Instagram found, keys saved.
No network.

Run:  python -m unittest tests.test_meta_login
"""
import os
import tempfile
import unittest
from unittest import mock


class R:
    def __init__(self, data, code=200):
        self._d, self.status_code, self.content = data, code, b"x"

    def json(self):
        return self._d


def fake_get(url, params=None, headers=None, timeout=None):
    if url.endswith("debug_token"):
        return R({"data": {"is_valid": True, "app_id": 42, "expires_at": 123,
                           "scopes": ["instagram_basic", "instagram_content_publish", "pages_show_list",
                                      "pages_read_engagement", "pages_manage_posts"]}})
    if url.endswith("me/accounts"):
        assert headers["Authorization"] == "Bearer LONG"
        return R({"data": [{"id": "1", "name": "Other page", "access_token": "P1"},
                           {"id": "2", "name": "Clips", "access_token": "PAGE",
                            "instagram_business_account": {"id": "99", "username": "clipz"}}]})
    return R({"username": "clipz"})


class MetaLoginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_paste_saves_all_four_keys(self):
        from clipfactory import meta_login, selftest
        env = os.path.join(self.tmp.name, ".env")
        with open(env, "w") as f:
            f.write("GEMINI_API_KEY=keep\nIG_MAIN_TOKEN=old\n")
        post = mock.Mock(return_value=R({"access_token": "LONG"}))
        with mock.patch("requests.get", side_effect=fake_get), mock.patch("requests.post", post), \
                mock.patch.object(selftest, "_check", return_value="OK"):
            lines = meta_login.connect("SHORT", secret="S", env_path=env)
        self.assertEqual(post.call_args.kwargs["data"]["client_secret"], "S")    # secret in the body, not the URL
        saved = dict(ln.split("=", 1) for ln in open(env).read().split())
        self.assertEqual(saved, {"GEMINI_API_KEY": "keep", "IG_MAIN_TOKEN": "PAGE", "IG_MAIN_USER_ID": "99",
                                 "FB_MAIN_PAGE_ID": "2", "FB_MAIN_PAGE_TOKEN": "PAGE", "META_APP_SECRET": "S"})
        self.assertIn("Instagram: @clipz", lines)
        self.assertFalse(any("PAGE" in ln or "LONG" in ln for ln in lines))      # no token is printed

    def test_missing_permission_is_named(self):
        from clipfactory import meta_login
        bad = mock.Mock(return_value=R({"data": {"is_valid": True, "app_id": 1, "scopes": ["pages_show_list"]}}))
        with mock.patch("requests.get", bad), self.assertRaises(RuntimeError) as e:
            meta_login.connect("T", env_path=os.path.join(self.tmp.name, ".env"))
        self.assertIn("instagram_content_publish", str(e.exception))


if __name__ == "__main__":
    unittest.main()
