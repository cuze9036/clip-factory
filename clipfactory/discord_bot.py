"""Discord bot: posts every alert from the outbox to your channel, and takes your commands and button taps.
Only the owner (DISCORD_OWNER_ID) in the control channel (DISCORD_CHANNEL_ID) can control it."""
import asyncio
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
        if (a.filename or "").lower().endswith(".txt") and a.size <= 200_000:
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

    async def setup_hook(self):
        self.tree = discord.app_commands.CommandTree(self)
        for name, (about, box) in commands.SLASH.items():
            self.tree.add_command(self._slash(name, about, box))
        self.loop.create_task(self.drain_outbox())

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
        await ch.send(f"Clip Factory is running (version {commands.version()}). Type `/` or `!help` for commands.")
        if ch.guild:
            try:   # a server-only sync shows the / menu right away
                self.tree.copy_global_to(guild=ch.guild)
                await self.tree.sync(guild=ch.guild)
            except discord.HTTPException as e:
                db.log(None, f"Slash commands not registered: {e}", "warn")
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
                    msg = await ch.send(row["text"], **kwargs)
                    notify.mark_sent(row["id"], str(msg.id))
                except Exception as e:
                    notify.mark_sent(row["id"], error=str(e)[:300])
                    db.log(row["campaign_id"], f"Discord send failed: {e}", "warn")
            await asyncio.sleep(5)

    def _allowed(self, user_id, channel_id):
        return user_id == self.owner_id and channel_id == self.channel_id

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
            if not cid or len(text) < 80:
                return
            async with message.channel.typing():   # the pasted campaign page after "I joined"
                reply = await asyncio.to_thread(commands.brief, cid, text)
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
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass
        await interaction.followup.send(reply)


def run():
    Bot().run(config.env("DISCORD_BOT_TOKEN"), log_handler=None)
