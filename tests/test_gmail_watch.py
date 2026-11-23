"""Gmail audit watcher, tested against a fake Gmail (no network, no Google login).

Run:  python -m unittest tests.test_gmail_watch
"""
import base64
import os
import tempfile
import unittest
from unittest import mock


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class _Call:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeMessages:
    """Only list() and get() exist, so any modify/trash/delete/send call would fail the test."""

    def __init__(self, emails):
        self.emails = emails
        self.get_calls = 0

    def list(self, userId, q, maxResults):
        assert userId == "me" and "newer_than:7d" in q
        return _Call({"messages": [{"id": i} for i in self.emails][:maxResults]})

    def get(self, userId, id, format):
        self.get_calls += 1
        return _Call(self.emails[id])


class FakeGmail:
    def __init__(self, emails):
        self.messages_api = FakeMessages(emails)

    def users(self):
        gmail = self

        class Users:
            def messages(self):
                return gmail.messages_api
        return Users()


def _email(subject, body, html=False, ms=None):
    import time
    part = {"mimeType": "text/html" if html else "text/plain", "body": {"data": _b64(body)}}
    return {"internalDate": str(ms or int(time.time() * 1000)), "snippet": body[:50],
            "payload": {"headers": [{"name": "Subject", "value": subject},
                                    {"name": "Date", "value": "Sat, 22 Nov 2026 10:00:00 +0000"},
                                    {"name": "From", "value": "someone@example.com"}],
                        "mimeType": "multipart/alternative", "parts": [part]}}


class GmailWatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["CLIPFACTORY_DATA"] = self.tmp.name
        from clipfactory import db
        db._local.__dict__.clear()   # fresh database per test

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, svc, summary):
        from clipfactory import gmail_watch, llm
        with mock.patch.object(llm, "complete_json", return_value=(summary, "fake")) as m:
            n = gmail_watch.check(svc=svc)
        return n, m

    def test_new_email_goes_to_discord_once(self):
        from clipfactory import db
        svc = FakeGmail({"a1": _email("YouTube API Services - Audit", "Please reply by December 5 with a video.")})
        n, m = self._run(svc, {"summary": "Google needs a screencast.", "action": "Reply with a screencast link",
                               "deadline": "December 5, 2026"})
        self.assertEqual(n, 1)
        rows = db.q("SELECT * FROM outbox")
        self.assertEqual(len(rows), 1)
        text = rows[0]["text"]
        self.assertEqual(rows[0]["kind"], "audit")
        self.assertTrue(text.startswith("**Deadline: December 5, 2026**\n**Action required: Reply with a screencast link**"))
        self.assertIn("Subject: YouTube API Services - Audit", text)
        self.assertNotIn("@example.com", text)          # no email addresses on Discord
        # second run: already seen, nothing new and the email isn't even fetched again
        n2, _ = self._run(svc, {"summary": "x", "action": "", "deadline": ""})
        self.assertEqual(n2, 0)
        self.assertEqual(svc.messages_api.get_calls, 1)
        self.assertEqual(len(db.q("SELECT * FROM outbox")), 1)

    def test_no_action_means_no_bold_header(self):
        from clipfactory import db
        svc = FakeGmail({"b1": _email("Your quota", "<p>Your project <b>quota</b> is fine.</p>", html=True)})
        _, m = self._run(svc, {"summary": "Quota is fine.", "action": "", "deadline": ""})
        self.assertIn("Your project quota is fine.", m.call_args[0][1])   # HTML was turned into text for the AI
        text = db.q("SELECT text FROM outbox")[0]["text"]
        self.assertTrue(text.startswith("📧 Google audit email"))

    def test_llm_down_falls_back_to_email_text(self):
        from clipfactory import gmail_watch, llm
        svc = FakeGmail({"c1": _email("Audit update", "We received your form.")})
        with mock.patch.object(llm, "complete_json", side_effect=llm.LLMError("down")):
            self.assertEqual(gmail_watch.check(svc=svc), 1)
        self.assertEqual(gmail_watch.recent(7)[0]["summary"], "We received your form.")

    def test_not_connected_does_not_crash(self):
        from clipfactory import gmail_watch, selftest
        self.assertFalse(gmail_watch.token_path().exists())
        self.assertEqual(gmail_watch.check(), 0)
        self.assertIn("not connected", gmail_watch.emails_command())
        line = selftest._check("Gmail audit watcher", selftest._gmail)
        self.assertTrue(line.startswith("SKIP  Gmail audit watcher: not connected"))

    def test_emails_command_lists_last_7_days(self):
        import time
        from clipfactory import gmail_watch
        old = int((time.time() - 10 * 86400) * 1000)
        svc = FakeGmail({"n1": _email("Audit approved", "Approved."), "o1": _email("Old audit", "Old.", ms=old)})
        self._run(svc, {"summary": "Approved.", "action": "", "deadline": ""})
        with mock.patch.object(gmail_watch, "token_path") as tp, mock.patch.object(gmail_watch, "check"):
            tp.return_value.exists.return_value = True
            reply = gmail_watch.emails_command()
        self.assertIn("Audit approved", reply)
        self.assertNotIn("Old audit", reply)

    def test_only_read_only_scope(self):
        from clipfactory import gmail_watch
        self.assertEqual(gmail_watch.SCOPES, ["https://www.googleapis.com/auth/gmail.readonly"])


if __name__ == "__main__":
    unittest.main()
