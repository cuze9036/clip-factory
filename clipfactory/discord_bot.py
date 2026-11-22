"""Discord bot: posts every alert from the outbox to your channel, and takes your commands and button taps.
Only the owner (DISCORD_OWNER_ID) in the control channel (DISCORD_CHANNEL_ID) can control it."""
import asyncio
import datetime
import os
import subprocess

import discord

from . import commands, config, db, notify

STYLES = {"green": discord.ButtonStyle.success, "red": discord.ButtonStyle.danger, "grey": discord.ButtonStyle.secondary}
MAX_UPLOAD = 9 * 1024 * 1024  # Discord's default upload limit is 10 MB


def configured():
    return all(config.env(k) for k in ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_OWNER_ID"))


def preview(path):
    """A small 540x960 copy of the clip so it fits Discord's upload limit."""
    if not path or not os.path.exists(path):
        return None
    out = config.data_dir() / "previews" / (os.path.basename(path).replace(".mp4", "_preview.mp4"))
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-vf", "scale=540:-2", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
                        str(out)], check=True)
    return str(out) if out.stat().st_size <= MAX_UPLOAD else None


def view_for(buttons):
    if not buttons:
        return None
    v = discord.ui.View(timeout=None)
    for label, custom_id, style in buttons[:20]:
        if style == "link":   # opens a web page; custom_id holds the URL
            v.add_item(discord.ui.Button(label=label[:80], url=custom_id, style=discord.ButtonStyle.link))
        else:
            v.add_item(discord.ui.Button(label=label[:80], custom_id=custom_id,
                                         style=STYLES.get(style, STYLES["grey"])))
    return v


async def full_text(message):
    """Message text plus any pasted .txt file (Discord turns long pastes into message.txt) and embeds."""
    parts = [message.content or ""]
    for a in message.attachments:
        name, ctype = (a.filename or "").lower(), (a.content_type or "").lower()
        if (name.endswith((".txt", ".md", ".csv")) or ctype.startswith("text/")) and a.size <= 1_000_000:
            parts.append((await a.read()).decode("utf-8", "replace"))
    for e in message.embeds:
        parts += [e.title or "", e.description or "", e.url or ""]
        parts += [f"{f.name}: {f.value}" for f in e.fields]
    return "\n".join(p for p in parts if p).strip()


class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.channel_id = int(config.env("DISCORD_CHANNEL_ID"))
        self.owner_id = int(config.env("DISCORD_OWNER_ID"))
        self.feed_id = None
        self.board_ids = {}
        self.insights_id = None   # #ai-insights: weekly review and daily check

    async def setup_hook(self):
        self.tree = discord.app_commands.CommandTree(self)
        for name, (about, box) in commands.SLASH.items():
            self.tree.add_command(self._slash(name, about, box))
        self.loop.create_task(self.drain_outbox())
        self.loop.create_task(self.heartbeat())
        self.loop.create_task(self.board_loop())

    def _slash(self, name, about, box):
        """A real Discord slash command (shows up in the / menu) that runs the same as typing !name."""
        async def run(interaction, details: str = ""):
            if not self._allowed(interaction.user.id, interaction.channel_id):
                await interaction.response.send_message("Only the owner can use this here.", ephemeral=True)
                return
            await interaction.response.defer(thinking=True)
            reply = await asyncio.to_thread(commands.handle, f"!{name} {details}".strip()) or "Done."
            for i in range(0, len(reply), 1900):
                await interaction.followup.send(reply[i:i + 1900])

        if box is None:
            async def callback(interaction: discord.Interaction):
                await run(interaction)
        else:
            async def callback(interaction: discord.Interaction, details: str = ""):
                await run(interaction, details)
            callback = discord.app_commands.describe(details=box)(callback)
        return discord.app_commands.Command(name=name, description=about[:100], callback=callback)

    async def on_ready(self):
        db.log(None, f"Discord bot online as {self.user}")
        ch = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
        await ch.send(f"🟢 Clip Factory is running (version {commands.version()}). Type `/` or `!help` for commands.")
        if ch.guild:
            try:   # a server-only sync shows the / menu right away
                self.tree.copy_global_to(guild=ch.guild)
                await self.tree.sync(guild=ch.guild)
            except discord.HTTPException as e:
                db.log(None, f"Slash commands not registered: {e}", "warn")
        await self.find_board(ch)
        await self.find_insights(ch)
        name = (config.section("discord").get("feed_channel") or "").lstrip("#")
        feed = discord.utils.get(ch.guild.text_channels, name=name) if name and ch.guild else None
        if feed:
            self.feed_id = feed.id
            try:   # catch up on what was posted while the tool was off
                async for m in feed.history(limit=30, oldest_first=True):
                    await self.read_feed(m)
            except discord.HTTPException as e:
                db.log(None, f"Couldn't read #{name}: {e}", "warn")

    async def read_feed(self, message):
        if message.author == self.user:
            return
        text = await full_text(message)
        try:
            from . import joinflow
            await asyncio.to_thread(joinflow.from_feed, text, message.id)
        except Exception as e:
            db.log(None, f"#campaign-feed message not read: {str(e)[:200]}", "warn")

    # ---------- campaign boards: #campaign-updates and #active-campaigns ----------

    async def find_board(self, ch):
        """Use the channels that already exist; never create one."""
        from . import board
        if not ch.guild:
            return
        names = [c.name for c in ch.guild.text_channels]
        missing = []
        for kind, wanted in board.channel_names().items():
            found = board.match_channel(names, wanted)
            if found:
                self.board_ids[kind] = discord.utils.get(ch.guild.text_channels, name=found).id
            else:
                missing.append(f"#{wanted}")
        if missing and not db.kv_get("board_missing_noted"):
            db.kv_set("board_missing_noted", True)
            await ch.send(f"I couldn't find {' and '.join(missing)}, so that board is off. "
                          "Create the channel (or set its name in config.local.yaml) and restart.")

    async def find_insights(self, ch):
        """#ai-insights for the weekly review and the daily check; made once if it doesn't exist."""
        from . import board
        if not ch.guild:
            return
        name = (config.section("discord").get("insights_channel") or "ai-insights").lstrip("#")
        found = board.match_channel([c.name for c in ch.guild.text_channels], name)
        chan = discord.utils.get(ch.guild.text_channels, name=found) if found else None
        if not chan:
            try:
                chan = await ch.guild.create_text_channel(
                    name, category=ch.category, topic="Clip Factory's own review: what works, what doesn't, problems to fix")
            except discord.HTTPException:
                if not db.kv_get("insights_channel_noted"):
                    db.kv_set("insights_channel_noted", True)
                    await ch.send(f"Create a text channel named #{name} and I'll post the weekly review and the "
                                  "daily check there (until then they come here).")
                return
        self.insights_id = chan.id

    async def board_loop(self):
        await self.wait_until_ready()
        from . import board
        tick = 0
        while not self.is_closed():
            try:
                dirty = await asyncio.to_thread(db.kv_get, "board_dirty")
                if self.board_ids and (dirty or tick % 4 == 0):   # every 2 minutes, or right after a button tap
                    await asyncio.to_thread(db.kv_set, "board_dirty", None)
                    plans = await asyncio.to_thread(board.plan)
                    for kind, chan_id in list(self.board_ids.items()):
                        await self.refresh_board(board, kind, chan_id, plans[kind])
            except Exception as e:
                db.log(None, f"Campaign board refresh failed: {str(e)[:200]}", "warn")
            tick += 1
            await asyncio.sleep(30)

    async def refresh_board(self, board, kind, chan_id, desired):
        chan = self.get_channel(chan_id) or await self.fetch_channel(chan_id)
        rows = await asyncio.to_thread(board.current, kind)
        op, keys = board.ops(desired, rows)
        summary = f"{kind}:summary"
        if op == "edit" and board.due(kind) and summary not in keys:
            keys.append(summary)
        if op == "edit":
            by_key = {r["key"]: r for r in rows}
            for d in desired:
                if d["key"] not in keys:
                    continue
                try:
                    await chan.get_partial_message(int(by_key[d["key"]]["message_id"])).edit(
                        content=d["text"], view=view_for(d["buttons"]))
                    await asyncio.to_thread(db.x, "UPDATE board SET hash=? WHERE key=?", board.digest(d), d["key"])
                except discord.NotFound:   # someone deleted it: rebuild this board next time
                    await asyncio.to_thread(db.x, "DELETE FROM board WHERE key=?", d["key"])
                    await asyncio.to_thread(db.kv_set, "board_dirty", True)
            if summary in keys:
                await asyncio.to_thread(board.mark_full, kind)
            return
        # Repost: the set of campaigns or their priority changed, and Discord can't reorder messages.
        tracked = {r["message_id"] for r in rows}
        for r in rows:
            try:
                await chan.get_partial_message(int(r["message_id"])).delete()
            except discord.HTTPException:
                pass
        old = discord.utils.utcnow() - datetime.timedelta(minutes=30)
        async for m in chan.history(limit=100):   # tidy the bot's own old replies in this channel too
            if m.author == self.user and str(m.id) not in tracked and m.created_at < old:
                try:
                    await m.delete()
                except discord.HTTPException:
                    pass
        await asyncio.to_thread(db.x, "DELETE FROM board WHERE key LIKE ?", kind + ":%")
        for pos, d in enumerate(desired):
            msg = await chan.send(d["text"], view=view_for(d["buttons"]))
            await asyncio.to_thread(db.x, "INSERT OR REPLACE INTO board(key, message_id, hash, pos, tier) VALUES(?,?,?,?,?)",
                                    d["key"], str(msg.id), board.digest(d), pos, d["tier"])
        await asyncio.to_thread(board.mark_full, kind)

    async def heartbeat(self):
        """Every 30 s, tell the offline-alert service (HEARTBEAT_URL, e.g. healthchecks.io) the bot is alive.
        When the pings stop (laptop off, asleep, no internet, crash), that service posts "Bot is offline" on Discord
        from the cloud, and "Bot is online" when they start again."""
        await self.wait_until_ready()
        url = config.env("HEARTBEAT_URL")
        if not url:
            return
        import requests
        while not self.is_closed():
            try:
                await asyncio.to_thread(requests.get, url, timeout=10)
            except Exception:
                pass   # no internet right now; the service will notice and alert
            await asyncio.sleep(30)

    async def drain_outbox(self):
        await self.wait_until_ready()
        ch = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
        while not self.is_closed():
            for row in await asyncio.to_thread(notify.pending):
                try:
                    small = await asyncio.to_thread(preview, row["file_path"]) if row["file_path"] else None
                    kwargs = {}
                    if small:
                        kwargs["file"] = discord.File(small)
                    if row["buttons"]:
                        kwargs["view"] = view_for(row["buttons"])
                    target = ch
                    if row["kind"] in ("insights", "health") and self.insights_id:
                        target = self.get_channel(self.insights_id) or ch
                    msg = await target.send(row["text"], **kwargs)
                    notify.mark_sent(row["id"], str(msg.id))
                except Exception as e:
                    notify.mark_sent(row["id"], error=str(e)[:300])
                    db.log(row["campaign_id"], f"Discord send failed: {e}", "warn")
            await asyncio.sleep(5)

    def _allowed(self, user_id, channel_id):
        return user_id == self.owner_id and (channel_id in (self.channel_id, self.insights_id)
                                             or channel_id in self.board_ids.values())

    async def on_message(self, message):
        if self.feed_id and message.channel.id == self.feed_id:
            await self.read_feed(message)   # followed announcement posts arrive as bot/webhook messages
            return
        if message.author.bot or not self._allowed(message.author.id, message.channel.id):
            return
        text = await full_text(message)
        if not text.startswith(("!", "/")):
            from . import joinflow
            cid = await asyncio.to_thread(joinflow.awaiting)
            kind = await asyncio.to_thread(joinflow.awaiting_kind)
            linkish = text.startswith(("http", "<http", "@")) or ":\\" in text
            if not cid:
                if len(text) >= 200 or message.attachments or "docs.google.com/" in text:
                    await message.channel.send(   # don't guess which campaign a long paste is for
                        "Which campaign is this for? Tap **📋 Guide** (or **🎬 Footage**) on its card in "
                        "#active-campaigns, then send it again. It goes to that campaign only.")
                return
            if len(text) < {"guide": 20, "reedit": 3}.get(kind, 80) and not linkish:
                return
            async with message.channel.typing():   # the page after "I joined", or a guide / footage after a card button
                reply = await asyncio.to_thread(commands.pasted, cid, text)
            for i in range(0, len(reply), 1900):
                await message.channel.send(reply[i:i + 1900])
            return
        async with message.channel.typing():
            reply = await asyncio.to_thread(commands.handle, text)
        if reply:
            for i in range(0, len(reply), 1900):
                await message.channel.send(reply[i:i + 1900])

    async def on_interaction(self, interaction):
        if interaction.type != discord.InteractionType.component:
            return   # slash commands are handled by the command tree
        if not self._allowed(interaction.user.id, interaction.channel_id):
            await interaction.response.send_message("Only the owner can use these buttons.", ephemeral=True)
            return
        await interaction.response.defer()
        reply = await asyncio.to_thread(commands.button, interaction.data.get("custom_id", ""))
        if interaction.channel_id in self.board_ids.values():
            await asyncio.to_thread(db.kv_set, "board_dirty", True)   # board cards keep their buttons
        else:
            try:
                await interaction.message.edit(view=None)
            except discord.HTTPException:
                pass
        await interaction.followup.send(reply)


def run():
    Bot().run(config.env("DISCORD_BOT_TOKEN"), log_handler=None)
