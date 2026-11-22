"""Telegram only delivers the reels you post yourself (Instagram, Facebook): the video, then its caption.
Everything else (approving, marking posts live, commands) happens on Discord."""
import os
import subprocess

import requests

from . import config

MAX_UPLOAD = 49 * 1024 * 1024  # Telegram bots can send files up to 50 MB


def creds():
    token, chat = config.env("TELEGRAM_BOT_TOKEN"), config.env("TELEGRAM_CHAT_ID")
    return (f"https://api.telegram.org/bot{token}/", str(chat)) if token and chat else None


def _fit(path):
    """Under Telegram's 50 MB limit: the original, or a 720p copy if the original is too big."""
    if os.path.getsize(path) <= MAX_UPLOAD:
        return path
    out = config.data_dir() / "previews" / os.path.basename(path).replace(".mp4", "_720.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", path, "-vf", "scale=720:-2", "-c:v", "libx264", "-preset",
                        "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
                       check=True)
    return str(out)


def send_text(text):
    base, chat = creds()
    r = requests.post(base + "sendMessage", data={"chat_id": chat, "text": text[:4000],
                                                  "disable_web_page_preview": "true"}, timeout=60)
    result = r.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram refused the message"))


def send_video(path, caption=""):
    base, chat = creds()
    with open(_fit(path), "rb") as f:
        r = requests.post(base + "sendVideo", data={"chat_id": chat, "caption": caption[:1000],
                                                    "supports_streaming": "true"}, files={"video": f}, timeout=600)
    result = r.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram refused the video"))


def hand_off(post, clip, caption, campaign_name, when=""):
    """The video, then the caption on its own so it's easy to copy. Returns False when Telegram isn't set up."""
    if not creds():
        return False
    from .publish import name
    send_video(clip["final_path"], f"{name(post['platform'])} · post {post['id']} · {campaign_name}"
               + (f"\nPost at {when}" if when else ""))
    send_text(caption)
    return True
