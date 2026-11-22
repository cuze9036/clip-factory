"""Daily check for #ai-insights, and that reviews go to that channel. No network.

Run:  python -m unittest tests.test_health
"""
import asyncio
import datetime as dt
import os
import tempfile
import unittest
from unittest import mock


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(CLIPFACTORY_DATA=self.tmp.name, DISCORD_CHANNEL_ID="1", DISCORD_OWNER_ID="2",
                          DISCORD_BOT_TOKEN="x")
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_fine(self):
        from clipfactory import health
        self.assertIn("✅ Everything looks fine.", health.report())

    def test_finds_problems_with_fixes(self):
        from clipfactory import campaigns, db, health
        from clipfactory.util import iso, utcnow
        old = iso(utcnow() - dt.timedelta(hours=30))
        cid = campaigns.suggest({"name": "Band Live", "platforms": ["instagram"], "rate_per_1k": 2,
                                 "budget_left": 300}, "pasted")
        campaigns.activate(cid)
        db.x("INSERT INTO events(at, level, msg) VALUES(?,?,?)", iso(utcnow()), "error", "Render failed for clip 4: x")
        db.x("INSERT INTO clips(campaign_id, status, created_at) VALUES(?,?,?)", cid, "pending_review", old)
        k = db.x("INSERT INTO clips(campaign_id, status, created_at) VALUES(?,?,?)", cid, "scheduled", old)
        db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, scheduled_at, status, mode) VALUES(?,?,?,?,?,?,?)",
             k, cid, "tiktok", "main", old, "manual", "manual")
        text = health.report()
        self.assertIn("🔧 Error ×1: Render failed for clip 4", text)
        self.assertIn("👀 1 clip(s) waiting for your review", text)
        self.assertIn("📱 1 hand-post(s) not made yet", text)
        self.assertIn("💰 Band Live: only $300 budget left", text)
        self.assertIn("🎬 Band Live: no footage yet", text)
        self.assertIn("→ See `!todo`", text)

    def test_reviews_go_to_ai_insights(self):
        from clipfactory import discord_bot, notify

        class Chan:
            def __init__(self):
                self.sent = []

            async def send(self, text, **kw):
                self.sent.append(text)
                return mock.Mock(id=1)

        general, insights = Chan(), Chan()
        notify.send("health", "🩺 Daily check")
        notify.send("manual", "⏰ Time to post")

        async def run():
            bot = discord_bot.Bot()
            bot.insights_id = 9
            calls = {"n": 0}

            def closed():
                calls["n"] += 1
                return calls["n"] > 1

            with mock.patch.object(bot, "wait_until_ready", mock.AsyncMock()), \
                    mock.patch.object(bot, "get_channel", side_effect=lambda i: insights if i == 9 else general), \
                    mock.patch.object(bot, "is_closed", closed), mock.patch("asyncio.sleep", mock.AsyncMock()):
                await bot.drain_outbox()

        asyncio.run(run())
        self.assertEqual(insights.sent, ["🩺 Daily check"])
        self.assertEqual(general.sent, ["⏰ Time to post"])


if __name__ == "__main__":
    unittest.main()
