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
        v.add_item(discord.ui.Button(label=label[:80], custom_id=custom_id, style=STYLES.get(style, STYLES["grey"])))
    return v


class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.channel_id = int(config.env("DISCORD_CHANNEL_ID"))
        self.owner_id = int(config.env("DISCORD_OWNER_ID"))

    async def setup_hook(self):
        self.loop.create_task(self.drain_outbox())

    async def on_ready(self):
        db.log(None, f"Discord bot online as {self.user}")
        ch = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
        await ch.send(f"Clip Factory is running (version {commands.version()}). Type `!help` for commands.")

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
        if message.author.bot or not self._allowed(message.author.id, message.channel.id):
            return
        async with message.channel.typing():
            reply = await asyncio.to_thread(commands.handle, message.content)
        if reply:
            for i in range(0, len(reply), 1900):
                await message.channel.send(reply[i:i + 1900])

    async def on_interaction(self, interaction):
        if interaction.type != discord.InteractionType.component:
            return
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
