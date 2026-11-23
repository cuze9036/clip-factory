"""Checks every part of the tool on this computer and returns a short report."""
import os
import shutil
import subprocess
import tempfile
import time

import requests

from . import config, llm


def _check(name, fn):
    t = time.time()
    try:
        note = fn() or "works"
        return f"OK    {name}: {note} ({time.time() - t:.0f}s)"
    except _Skip as e:
        return f"SKIP  {name}: {e}"
    except Exception as e:
        return f"FAIL  {name}: {str(e)[:160]}"


class _Skip(Exception):
    pass


def _tool(cmd):
    def run():
        if not shutil.which(cmd):
            raise RuntimeError(f"{cmd} not found; run the installer again")
        return "installed"
    return run


def _gemini():
    ps = llm.providers(video=True)
    if not ps:
        raise _Skip("GEMINI_API_KEY not set")
    llm.parse_json(llm._gemini(ps[0], 'Reply with this JSON only: {"ok": true}', None))
    return f"model {ps[0]['model']} answered"


def _free_llm():
    ps = [p for p in llm.providers() if p["type"] == "openai" and not p.get("optional")]
    if not ps:
        raise _Skip("FREE_LLM_* not set")
    llm.parse_json(llm._openai(ps[0], 'Reply with this JSON only: {"ok": true}'))
    return f"model {ps[0]['model']} answered"


def _youtube_key():
    from . import youtube_api
    if not config.env("YOUTUBE_API_KEY"):
        raise _Skip("YOUTUBE_API_KEY not set")
    youtube_api.resolve_channel("@YouTube")
    return "can find channels and streams"


def _youtube_login():
    from . import publish
    acc = (config.get().get("accounts") or {}).get("youtube", {}).get("main", {})
    if not config.path(acc.get("token_file", "secrets/youtube_main.json")).exists():
        raise _Skip("not connected; run: clipfactory auth-youtube main")
    publish.youtube_creds("main")
    return "upload login is valid"


def _telegram():
    from . import telegram
    c = telegram.creds()
    if not c:
        raise _Skip("TELEGRAM_* not set")
    try:
        r = requests.get(c[0] + "getMe", timeout=20).json()
    except Exception as e:   # the URL holds the token, so show only the error type
        raise RuntimeError(f"could not reach Telegram ({type(e).__name__})") from None
    if not r.get("ok"):
        raise RuntimeError(r.get("description", "token refused"))
    return f"bot @{r['result']['username']}"


def _discord():
    for k in ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_OWNER_ID"):
        if not config.env(k):
            raise RuntimeError(f"{k} is empty")
    return "keys set"


def _apify():
    token = config.env("APIFY_TOKEN")
    if not token:
        raise _Skip("APIFY_TOKEN not set (campaign finder off)")
    r = requests.get("https://api.apify.com/v2/users/me", params={"token": token}, timeout=20)
    if r.status_code >= 400:   # not raise_for_status: its message would show the token
        raise RuntimeError(f"Apify returned HTTP {r.status_code}")
    return f"account {r.json()['data'].get('username', 'ok')}"


def _download():
    from yt_dlp import YoutubeDL
    with YoutubeDL({"quiet": True, "skip_download": True}) as y:
        info = y.extract_info("https://www.youtube.com/watch?v=jNQXAC9IVRw", download=False)
    return f"can read YouTube videos ({info.get('duration')}s test video)"


def _font():
    want = config.section("render").get("font", "Montserrat ExtraBold").split()[0].lower()
    dirs = [os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
            str(config.path(config.section("render").get("fonts_dir", "assets/fonts"))),
            "/usr/share/fonts", os.path.expanduser("~/.fonts")]
    for d in dirs:
        for root, _, files in os.walk(d) if os.path.isdir(d) else []:
            if any(want in f.lower() for f in files):
                return f"{want.title()} found"
    raise RuntimeError(f"{want.title()} not installed; captions use a plain fallback font "
                       "(install Montserrat from Google Fonts)")


def _render():
    from . import render, transcribe
    from .util import run
    with tempfile.TemporaryDirectory() as d:
        src, out = os.path.join(d, "src.mp4"), os.path.join(d, "out.mp4")
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30", "-f", "lavfi",
             "-i", "sine=frequency=300:sample_rate=48000", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
             "-c:a", "aac", src])
        transcribe.words(src)  # downloads the speech model the first time
        words = [{"w": w, "s": i * 0.5 + 0.3, "e": i * 0.5 + 0.7} for i, w in enumerate("this is a test clip".split())]
        cfg = config.section("render")
        old = cfg.get("preset")
        cfg["preset"] = "ultrafast"
        try:
            render.render(src, out, words, "Self test", "blur", None)
        finally:
            cfg["preset"] = old
        info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height", "-of", "csv=p=0", out], capture_output=True, text=True).stdout
        if info.strip() != "1080,1920":
            raise RuntimeError(f"unexpected size {info.strip()}")
    return "speech model loaded, 1080x1920 clip with captions made"


def _gpu():
    import shutil as sh
    from . import db, render, transcribe
    if not sh.which("nvidia-smi"):
        raise _Skip("no NVIDIA GPU found; everything runs on the CPU")
    speech = "CPU (GPU libraries missing)" if db.kv_get("whisper_cpu_only") else "GPU"
    video = "GPU (NVENC)" if render.use_nvenc(config.section("render")) else "CPU"
    pause = db.kv_get("nvenc_pause_until")
    if video == "CPU" and pause:
        video += (f" for now (GPU paused after an error, tries again after {pause[11:16]} UTC: "
                  f"{(db.kv_get('nvenc_last_error') or '')[-120:]})")
    if not transcribe._nvidia_dirs():
        speech += "; run the installer again to add the GPU libraries"
    return f"captions on {speech}, video encoding on {video}"


def _heartbeat():
    url = config.env("HEARTBEAT_URL")
    if not url:
        raise _Skip("HEARTBEAT_URL not set, so Discord won't hear when the laptop goes offline")
    try:
        r = requests.get(url, timeout=15)
    except Exception as e:   # the URL is private, so show only the error type
        raise RuntimeError(f"could not reach the alert service ({type(e).__name__})") from None
    if r.status_code != 200:
        raise RuntimeError(f"alert service answered HTTP {r.status_code}")
    return "pinging; Discord hears 'Bot is offline' within about 2 minutes of the laptop stopping"


def _meta(platform):
    """Instagram / Facebook posting keys: reads the account's name with the saved token (nothing is posted)."""
    def check():
        from . import publish
        acc = publish._account(platform, "main")
        id_env = acc["user_id_env"] if platform == "instagram" else acc["page_id_env"]
        oid, token = publish.meta_keys(platform, "main")
        if not oid or not token:
            mode = publish.mode(platform, "main")
            raise _Skip(f"{id_env} / {acc['token_env']} not set, so you post {platform.title()} clips yourself"
                        + ("" if mode == "manual" else " (and it isn't in manual_platforms)"))
        field = "username" if platform == "instagram" else "name"
        try:   # the token goes in a header, never in the URL, so no error message can show it
            r = requests.get(publish._graph(oid), params={"fields": field},
                             headers={"Authorization": f"Bearer {token}"}, timeout=20)
        except Exception as e:
            raise RuntimeError(f"could not reach Meta ({type(e).__name__})") from None
        data = r.json() if r.headers.get("content-type", "").startswith(("application/json", "text/javascript")) else {}
        if r.status_code != 200 or field not in data:
            err = (data.get("error") or {}).get("message", f"HTTP {r.status_code}")
            raise RuntimeError(f"Meta refused the key: {err[:150]}")
        who = ("@" if platform == "instagram" else "") + str(data[field])
        return f"{who}: posts go out by themselves"
    return check


def _drive():
    from . import gdrive
    try:
        return gdrive.selftest_line()
    except gdrive.NeedsLogin as e:
        raise _Skip(str(e))


def _bugs():
    from . import bugreport
    if not bugreport.configured():
        raise _Skip("GITHUB_ISSUES_TOKEN not set, so code bugs aren't reported to Claude Code by themselves")
    return bugreport.selftest_line()


def _gmail():
    from . import gmail_watch
    try:
        return gmail_watch.selftest_line()
    except gmail_watch.NotConnected as e:
        raise _Skip(str(e))


CHECKS = [
    ("FFmpeg", _tool("ffmpeg")), ("Deno (for YouTube downloads)", _tool("deno")),
    ("Gemini", _gemini), ("Free LLM API", _free_llm), ("YouTube API key", _youtube_key),
    ("YouTube upload login", _youtube_login), ("Discord", _discord), ("Telegram", _telegram),
    ("Apify (campaign finder)", _apify), ("Reading YouTube videos", _download), ("Caption font", _font),
    ("Making a clip (first run downloads the speech model, about 500 MB)", _render),
    ("GPU", _gpu), ("Gmail audit watcher", _gmail), ("Offline alert", _heartbeat),
    ("Instagram posting", _meta("instagram")), ("Facebook posting", _meta("facebook")),
    ("Bug reports to Claude Code", _bugs), ("Google Drive (private campaign folders)", _drive),
]


def run_all():
    lines = [_check(name, fn) for name, fn in CHECKS]
    fails = sum(l.startswith("FAIL") for l in lines)
    head = "Self-test: everything works." if not fails else f"Self-test: {fails} problem(s) found."
    return head + "\n```\n" + "\n".join(lines) + "\n```"
