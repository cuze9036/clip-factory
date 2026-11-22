"""Campaign boards (#campaign-updates, #active-campaigns): priority tags, duplicates, order, edit vs repost, and the Discord sync (fake channel).

Run:  python -m unittest tests.test_board
"""
import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock


def _listing(title, rate=2.0, budget=12000, clippers=9):
    return {"title": title, "rewardPerThousandUsd": rate, "budgetLeftUsd": budget, "creatorsCount": clippers,
            "socialPlatforms": ["youtube", "instagram"], "payoutType": "cpm"}


EASY = ({"effort": "easy", "why": "long concert streams with many moments"}, "fake")


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(CLIPFACTORY_DATA=self.tmp.name, DISCORD_CHANNEL_ID="1", DISCORD_OWNER_ID="2",
                          DISCORD_BOT_TOKEN="x")
        from clipfactory import db
        db._local.__dict__.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _found(self, title, **kw):
        from clipfactory import discovery
        item = _listing(title, **kw)
        cid, _ = discovery.add_from_data(discovery.from_listing(item), json.dumps(item), "listing fields",
                                         "name:" + title, notify_always=False)
        return cid

    def _plan(self, effort=EASY, kind="live"):
        from clipfactory import board, llm
        with mock.patch.object(llm, "complete_json", return_value=effort) as m:
            return board.plan()[kind], m

    def test_priorities_order_and_summary(self):
        great = self._found("Great Clipping", rate=2.5, budget=20000, clippers=10)
        okay = self._found("Okay Clipping", rate=1.0, budget=3000, clippers=20)
        weak = self._found("Weak Clipping", rate=0.6, budget=600, clippers=50)
        items, _ = self._plan()
        tiers = {i["key"]: i["tier"] for i in items}
        self.assertEqual(tiers[f"live:c:{great}"], "first")
        self.assertEqual(tiers[f"live:c:{okay}"], "second")
        self.assertEqual(tiers[f"live:c:{weak}"], "low")
        self.assertEqual([i["key"] for i in items], [f"live:c:{weak}", f"live:c:{okay}", f"live:c:{great}", "live:summary"])
        great_card = items[2]["text"]
        self.assertTrue(great_card.startswith("**🥇 FIRST PRIORITY**"))
        self.assertIn("Effort: 🟢 easy", great_card)
        self.assertIn("~$2K each", great_card)
        self.assertEqual(items[2]["buttons"][1], ["I joined", f"join:{great}", "green"])
        self.assertIn("🥇 1 first · 🥈 1 second · ⚪ 1 low", items[-1]["text"])
        self.assertTrue(items[-1]["text"].startswith("**Campaign updates**"))

    def test_hard_campaign_is_low_and_ai_is_asked_once(self):
        from clipfactory import board
        cid = self._found("Brand Shoot", rate=3.0, budget=50000, clippers=5)
        items, m = self._plan(({"effort": "hard", "why": "needs original product filming"}, "fake"))
        self.assertEqual({i["key"]: i["tier"] for i in items}[f"live:c:{cid}"], "low")
        self.assertEqual(m.call_count, 1)
        _, m2 = self._plan()
        self.assertEqual(m2.call_count, 0)                 # saved on the campaign, not asked again
        self.assertTrue(board.plan())

    def test_ai_limit_per_refresh(self):
        for n in range(8):
            self._found(f"Campaign {n}")
        _, m = self._plan()
        self.assertEqual(m.call_count, 5)
        _, m = self._plan()
        self.assertEqual(m.call_count, 3)

    def test_duplicates_keep_the_freshest(self):
        from clipfactory import campaigns, db
        old = self._found("Band Live Clipping")
        new = campaigns.suggest({"name": "Band Live Clipping!", "platforms": ["youtube"], "rate_per_1k": 2,
                                 "budget_left": 9000}, "feed")
        db.x("UPDATE campaigns SET seen_at='2020-01-01T00:00:00Z' WHERE id=?", old)
        db.x("UPDATE campaigns SET origin='feed', found_at=? WHERE id=?", campaigns.iso(campaigns.utcnow()), new)
        items, _ = self._plan()
        keys = [i["key"] for i in items]
        self.assertIn(f"live:c:{new}", keys)
        self.assertNotIn(f"live:c:{old}", keys)

    def test_running_lanes_go_to_active_campaigns(self):
        from clipfactory import campaigns
        live = self._found("Live One")
        run = self._found("Running One")
        campaigns.activate(run)
        items, _ = self._plan()
        self.assertEqual([i["key"] for i in items], [f"live:c:{live}", "live:summary"])
        active, _ = self._plan(kind="running")
        self.assertEqual([i["key"] for i in active], [f"running:c:{run}", "running:summary"])
        self.assertTrue(active[0]["text"].startswith("**▶ RUNNING**"))
        self.assertEqual([b[1] for b in active[0]["buttons"]], [f"guide:{run}", f"src:{run}", f"pause:{run}"])
        self.assertIn("Guide: none yet", active[0]["text"])
        self.assertIn(f"id `{run}`", active[0]["text"])
        self.assertIn("1 running", active[1]["text"])

    def test_template_lanes_without_videos_are_not_shown(self):
        from clipfactory import campaigns
        campaigns.save("streamer-gta6", {"name": "Streamer GTA 6 clips", "platforms": ["youtube"]}, "yaml")
        active, _ = self._plan(kind="running")
        self.assertEqual([i["key"] for i in active], ["running:summary"])
        self.assertIn("Nothing running yet", active[0]["text"])

    def test_channel_names_match_with_emoji_and_case(self):
        from clipfactory import board
        names = ["general", "campaign-feed", "📢・campaign-updates", "Active-Campaigns", "approved-clips"]
        self.assertEqual(board.match_channel(names, "campaign-updates"), "📢・campaign-updates")
        self.assertEqual(board.match_channel(names, "active-campaigns"), "Active-Campaigns")
        self.assertEqual(board.match_channel(["campaign-feed"], "campaign-updates"), None)
        self.assertEqual(board.match_channel(["active-campaign"], "active-campaigns"), "active-campaign")

    def test_ops_edit_vs_repost(self):
        from clipfactory import board, db
        a = self._found("Alpha Clipping")
        items, _ = self._plan()
        self.assertEqual(board.ops(items, [])[0], "repost")
        for pos, d in enumerate(items):
            db.x("INSERT INTO board(key, message_id, hash, pos, tier) VALUES(?,?,?,?,?)",
                 d["key"], str(100 + pos), board.digest(d), pos, d["tier"])
        items, _ = self._plan()
        self.assertEqual(board.ops(items, board.current("live")), ("edit", []))   # clock change alone isn't an edit
        db.x("UPDATE campaigns SET budget_left=11000 WHERE id=?", a)
        items, _ = self._plan()
        self.assertEqual(board.ops(items, board.current("live")), ("edit", [f"live:c:{a}"]))
        self._found("Beta Clipping")
        items, _ = self._plan()
        self.assertEqual(board.ops(items, board.current("live"))[0], "repost")

    def test_ignored_campaign_leaves_the_board(self):
        from clipfactory import campaigns
        a = self._found("Alpha Clipping")
        campaigns.ignore(a)
        items, _ = self._plan()
        self.assertEqual([i["key"] for i in items], ["live:summary"])

    def test_discord_sync_with_fake_channel(self):
        from clipfactory import board, db, discord_bot, llm
        a = self._found("Alpha Clipping")

        class Msg:
            def __init__(self, chan, mid, text):
                self.chan, self.id, self.text = chan, mid, text

            async def edit(self, content, view):
                self.chan.msgs[self.id].text = content
                self.chan.edits += 1

            async def delete(self):
                self.chan.msgs.pop(self.id, None)

        class Chan:
            def __init__(self):
                self.msgs, self.next, self.edits = {}, 1000, 0

            async def send(self, text, view=None):
                self.next += 1
                self.msgs[self.next] = Msg(self, self.next, text)
                return self.msgs[self.next]

            def get_partial_message(self, mid):
                return self.msgs[mid]

            async def _hist(self):
                for m in []:
                    yield m

            def history(self, limit):
                return self._hist()

        chan = Chan()

        async def run():
            bot = discord_bot.Bot()

            async def refresh():
                await bot.refresh_board(board, "live", 5, board.plan()["live"])

            with mock.patch.object(bot, "get_channel", return_value=chan), \
                    mock.patch.object(llm, "complete_json", return_value=EASY):
                await refresh()                                      # first time: posts everything
                self.assertEqual(len(chan.msgs), 2)
                await refresh()                                      # nothing changed: no edits
                self.assertEqual(chan.edits, 0)
                db.x("UPDATE campaigns SET budget_left=5000 WHERE id=?", a)
                await refresh()                                      # numbers changed: edited in place
                self.assertEqual(chan.edits, 1)
                self.assertEqual(len(chan.msgs), 2)
                self.assertIn("$5K left", [m.text for m in chan.msgs.values()][0])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
