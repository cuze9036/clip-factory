"""A helper posts the hand-posted clips (TikTok, X) for you, from a Discord channel (it can be in another server).

When `poster.channel_id` is set (config.local.yaml), each hand-posted clip goes there instead of Telegram: the video
(full quality, sized to fit Discord) with the posting time and two buttons, then the caption on its own (easy copy).
The helper posts it, taps ✅ Posted and pastes the link (or replies to the video with the link). The tool then marks
it live and sends you the usual "📌 Submit now" with the link in #clip-submission. ❌ Can't post tells you.
The helper can only press these buttons and send links there: no commands, nothing else.
"""
import os
import re
import subprocess

from . import config, db, notify
from .util import iso, utcnow

LINK = {"tiktok": re.compile(r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/\S+", re.I),
        "x": re.compile(r"https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/\S+", re.I),
        "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/\S+", re.I),
        "facebook": re.compile(r"https?://(?:www\.|m\.)?(?:facebook\.com|fb\.watch)/\S+", re.I),
        "youtube": re.compile(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+", re.I)}
HELP = ("👋 **How this channel works**\n"
        "1. A video arrives here with the platform and the time to post it ({zone} time), then its caption.\n"
        "2. Post the video on that platform at that time, with that caption (copy it exactly).\n"
        "3. Tap **✅ Posted** under the video and paste the link of your post. (Or reply to the video with the link.)\n"
        "4. Can't post it? Tap **❌ Can't post**.\n"
        "Links look like https://www.tiktok.com/@name/video/123… or https://x.com/name/status/123…")


def cfg():
    return config.section("poster")


def channel_id():
    try:
        return int(str(cfg().get("channel_id") or "0").strip() or 0)
    except ValueError:
        return 0


def helpers():
    return {int(x) for x in cfg().get("helper_ids") or [] if str(x).strip().isdigit()}


def handles(platform):
    """True when this platform's hand-posts go to the helper's channel."""
    return bool(channel_id()) and platform in (cfg().get("platforms") or ["tiktok", "x"])


def may_use(user_id, owner_id):
    hs = helpers()
    return user_id == owner_id or not hs or user_id in hs


def zone():
    """The helper's time zone (poster.timezone, Pakistan by default) and its plain name."""
    tz = cfg().get("timezone") or "Asia/Karachi"
    return tz, {"Asia/Karachi": "Pakistan", "Asia/Kolkata": "India"}.get(tz, tz.split("/")[-1].replace("_", " "))


def when(post):
    """The posting time on the helper's clock (posts are still planned for US viewers)."""
    from .util import own_time, parse_iso
    return own_time(parse_iso(post["scheduled_at"]), tz=zone()[0])


def deliver(post, clip, caption, campaign_name, _owner_time=None):
    from .publish import name
    notify.send("poster", f"🎬 **{name(post['platform'])} · post {post['id']}** · {campaign_name}\n"
                f"Post at **{when(post)}** ({zone()[1]} time), with the caption below.",
                campaign_id=post["campaign_id"], clip_id=post["clip_id"], post_id=post["id"],
                file_path=clip["final_path"],
                buttons=[["✅ Posted", f"pp:{post['id']}", "green"], ["❌ Can't post", f"pn:{post['id']}", "grey"]])
    notify.send("poster", caption, campaign_id=post["campaign_id"], post_id=post["id"])
    from .publish import cta_text
    cta = cta_text(post["campaign_id"], post["id"])
    if cta:   # the campaign wants this comment from the account right after posting
        notify.send("poster", f"💬 Right after posting, comment this from the account (first comment):\n{cta}",
                    campaign_id=post["campaign_id"], post_id=post["id"])


def remind(post, _owner_time=None):
    from .publish import name
    notify.send("poster", f"⏰ **Post {post['id']} on {name(post['platform'])} is due now** ({when(post)}, "
                f"{zone()[1]} time). Post it, then tap "
                "✅ Posted on its video and paste the link.", campaign_id=post["campaign_id"], post_id=post["id"])


def posted(post_id, text, who=""):
    """The helper's link for a post → live, and you get the submit reminder. Returns the reply for the helper."""
    from . import publish
    p = db.one("SELECT * FROM posts WHERE id=?", int(post_id))
    if not p:
        return f"There's no post {post_id}."
    rx = LINK.get(p["platform"])
    m = (rx.search(text or "") if rx else None) or re.search(r"https?://\S+", text or "")
    if not m or (rx and not rx.search(m.group(0))):
        return (f"That isn't a {publish.name(p['platform'])} link. Send the link of the post, like "
                + ("https://www.tiktok.com/@name/video/123…" if p["platform"] == "tiktok" else
                   "https://x.com/name/status/123…" if p["platform"] == "x" else "https://…"))
    url = m.group(0).rstrip(").,>")
    if p["status"] == "posted" and p["url"]:
        return f"Post {post_id} is already saved: {p['url']}"
    publish.mark_posted(p["id"], url)
    db.log(p["campaign_id"], f"Post {post_id} ({p['platform']}) posted by {who or 'the helper'}: {url}")
    return f"✅ Saved post {post_id}: {url}. Thanks!"


def cant(post_id, who=""):
    from .publish import name
    p = db.one("SELECT * FROM posts WHERE id=?", int(post_id))
    if not p:
        return f"There's no post {post_id}."
    db.x("UPDATE posts SET status='failed', error=? WHERE id=?", f"skipped: {who or 'the helper'} couldn't post it",
         p["id"])
    notify.send("manual", f"⚠️ {who or 'Your helper'} couldn't post post {post_id} ({name(p['platform'])}).",
                campaign_id=p["campaign_id"], post_id=p["id"])
    return f"OK, post {post_id} is skipped. The owner is told."


def expire(hours=None):
    """A clip the helper didn't post within 24 h of its time: its messages leave their channel, the post is skipped."""
    from .util import parse_iso
    import datetime as dt
    hours = hours or cfg().get("expire_hours", 24)
    gone = 0
    for p in db.q("""SELECT DISTINCT p.* FROM posts p JOIN outbox o ON o.post_id=p.id AND o.kind='poster'
                     WHERE p.status IN ('queued','manual') AND p.scheduled_at IS NOT NULL"""):
        if utcnow() - parse_iso(p["scheduled_at"]) < dt.timedelta(hours=hours):
            continue
        ids = [r["message_id"] for r in db.q("SELECT message_id FROM outbox WHERE kind='poster' AND post_id=? "
                                             "AND message_id IS NOT NULL", p["id"])]
        if ids:
            db.kv_set("discord_delete", (db.kv_get("discord_delete") or []) + [["poster", i] for i in ids])
        db.x("UPDATE outbox SET message_id=NULL, sent_at=COALESCE(sent_at, ?), error='expired' WHERE kind='poster' "
             "AND post_id=?", iso(utcnow()), p["id"])
        db.x("UPDATE posts SET status='failed', error=? WHERE id=?", f"skipped: not posted within {hours} h", p["id"])
        db.log(p["campaign_id"], f"Post {p['id']} ({p['platform']}) wasn't posted within {hours} h: removed from the "
               "helper's channel")
        gone += 1
    return gone


def post_for_message(message_id):
    """The post a message in the helper's channel belongs to (a reply to the video carries the link)."""
    r = db.one("SELECT post_id FROM outbox WHERE kind='poster' AND message_id=? AND post_id IS NOT NULL",
               str(message_id))
    return r["post_id"] if r else None


def share_copy(path, max_bytes):
    """The clip at full size when it fits Discord's upload limit, else a copy encoded to just fit (1080x1920,
    720x1280 for long clips), good enough to post."""
    if not path or not os.path.exists(path):
        return None
    if os.path.getsize(path) <= max_bytes:
        return path
    out = config.data_dir() / "previews" / os.path.basename(path).replace(".mp4", "_share.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and out.stat().st_size <= max_bytes:
        return str(out)
    from .util import ffprobe
    dur = max(1.0, ffprobe(path)["duration"])
    kbps = int(max_bytes * 8 * 0.92 / dur / 1000) - 128
    scale = "scale=1080:1920" if kbps >= 2500 else "scale=720:1280"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-vf", scale, "-c:v", "libx264", "-preset", "medium",
                    "-b:v", f"{max(kbps, 400)}k", "-maxrate", f"{max(kbps, 400)}k", "-bufsize", f"{2 * max(kbps, 400)}k",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)], check=True)
    return str(out) if out.stat().st_size <= max_bytes else None


def help_once():
    if channel_id() and not db.kv_get(f"poster_help:{channel_id()}"):
        db.kv_set(f"poster_help:{channel_id()}", iso(utcnow()))
        notify.send("poster", HELP.format(zone=zone()[1]))


def setup(ask=input):
    """`clipfactory poster-setup`: asks the channel ID (and the helper's ID), saves them in config.local.yaml."""
    import yaml
    print("Helper channel for TikTok / X clips.\n"
          "In Discord: Settings > Advanced > Developer Mode ON. Then right-click the helper's channel > Copy Channel ID.")
    ch = re.sub(r"\D", "", ask("Paste the channel ID, then press Enter: "))
    if not ch:
        print("Nothing pasted, nothing changed.")
        return 1
    print("Optional: right-click your helper's name > Copy User ID (only they can press ✅ Posted). "
          "Press Enter to let anyone in that channel do it.")
    helper = [int(h) for h in re.findall(r"\d{15,}", ask("Helper's user ID(s), or Enter: "))]
    path = config.ROOT / "config.local.yaml"
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["poster"] = {**(data.get("poster") or {}), "channel_id": ch, "helper_ids": helper,
                      "platforms": (data.get("poster") or {}).get("platforms") or ["tiktok", "x"]}
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"Saved in {path}. Clip Factory restarts by itself in about 10 seconds; the bot then posts a short\n"
          "'How this channel works' message in that channel. New TikTok / X clips go there from now on.")
    return 0
