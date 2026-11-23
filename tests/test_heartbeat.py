"""Offline alert: the bot pings HEARTBEAT_URL every 30 s; selftest reports it. No network.

Run:  python -m unittest tests.test_heartbeat
"""
import asyncio
import os
import tempfile
import unittest
from unittest import mock


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ.update(CLIPFACTORY_DATA=self.tmp.name, DISCORD_CHANNEL_ID="1", DISCORD_OWNER_ID="2",
                          DISCORD_BOT_TOKEN="x")

    def tearDown(self):
        os.environ.pop("HEARTBEAT_URL", None)
        self.tmp.cleanup()

    def test_pings_until_closed_and_survives_no_internet(self):
        from clipfactory import discord_bot
        os.environ["HEARTBEAT_URL"] = "https://hc-ping.example/abc"
        calls = []

        def fake_get(url, timeout):
            calls.append(url)
            if len(calls) == 1:
                raise OSError("no internet")   # must not stop the loop

        async def main():
            bot = discord_bot.Bot()
            closed = iter([False, False, True])
            with mock.patch.object(bot, "wait_until_ready", mock.AsyncMock()), \
                    mock.patch.object(bot, "is_closed", lambda: next(closed)), \
                    mock.patch("requests.get", fake_get), \
                    mock.patch("asyncio.sleep", mock.AsyncMock()):
                await bot.heartbeat()

        asyncio.run(main())
        self.assertEqual(calls, ["https://hc-ping.example/abc"] * 2)

    def test_no_url_means_no_pings(self):
        from clipfactory import discord_bot

        async def main():
            bot = discord_bot.Bot()
            with mock.patch.object(bot, "wait_until_ready", mock.AsyncMock()), \
                    mock.patch("requests.get") as get:
                await bot.heartbeat()
                return get.call_count

        self.assertEqual(asyncio.run(main()), 0)

    def test_selftest_line(self):
        from clipfactory import selftest
        self.assertTrue(selftest._check("Offline alert", selftest._heartbeat).startswith("SKIP"))
        os.environ["HEARTBEAT_URL"] = "https://hc-ping.example/secret-id"
        with mock.patch("requests.get", side_effect=OSError("down")):
            line = selftest._check("Offline alert", selftest._heartbeat)
        self.assertTrue(line.startswith("FAIL"))
        self.assertNotIn("secret-id", line)   # the private URL never shows up on Discord
        with mock.patch("requests.get", return_value=mock.Mock(status_code=200)):
            self.assertTrue(selftest._check("Offline alert", selftest._heartbeat).startswith("OK"))


if __name__ == "__main__":
    unittest.main()
