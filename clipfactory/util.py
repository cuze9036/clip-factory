import datetime as dt
import json
import re
import subprocess
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def own_tz():
    """The owner's time zone (owner_timezone, India): every time the owner reads is shown in it, while posts are
    still planned for US viewers in Eastern time."""
    from . import config
    try:
        return ZoneInfo(config.get().get("owner_timezone") or "Asia/Kolkata")
    except Exception:
        return ET


def own_time(t, now=None, day=True):
    """'today 8:30 PM', 'tomorrow 6:30 AM' or 'Thu 27 Nov, 8:30 AM' in the owner's time (one time, no ET)."""
    tz = own_tz()
    t = t.astimezone(tz)
    clock = f"{t.hour % 12 or 12}:{t:%M %p}"
    if not day:
        return clock
    today = (now or utcnow()).astimezone(tz).date()
    if t.date() == today:
        return f"today {clock}"
    if t.date() == today + dt.timedelta(days=1):
        return f"tomorrow {clock}"
    if t.date() == today - dt.timedelta(days=1):
        return f"yesterday {clock}"
    return f"{t:%a} {t.day} {t:%b}, {clock}"


def et_slot_own(hhmm, on=None):
    """An Eastern-time slot like '20:00' as the owner's clock time ('6:30 AM'), for today's date."""
    h, m = map(int, hhmm.split(":"))
    d = (on or utcnow()).astimezone(ET).date()
    return own_time(dt.datetime.combine(d, dt.time(h, m), tzinfo=ET), day=False)


def own_times_in(text):
    """'Instagram at 20:00 ET does best' → 'Instagram at 6:30 AM does best' (AI text written from ET numbers)."""
    return re.sub(r"\b(\d{1,2}):(\d{2}) ?ET\b", lambda m: et_slot_own(f"{m.group(1)}:{m.group(2)}"), text or "")


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(d):
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_ts(v):
    """Seconds from 90, '90s', '1:30' or '1:01:30'."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?s?", s):
        return float(s.rstrip("s"))
    parts = s.split(":")
    if not 2 <= len(parts) <= 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        raise ValueError(f"bad timestamp {v!r}")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def fmt_ts(sec):
    sec = int(round(sec or 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def iso_duration(s):
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", s or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def youtube_id(url):
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    return url if re.fullmatch(r"[A-Za-z0-9_-]{11}", url) else None


def ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    fps = 30.0
    if v and v.get("avg_frame_rate", "0/0") != "0/0":
        n, d = v["avg_frame_rate"].split("/")
        fps = float(n) / float(d) if float(d) else 30.0
    return {
        "duration": float(info["format"].get("duration", 0)),
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "fps": fps,
        "has_audio": a is not None,
    }


def run(cmd):
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr[-1500:]}")
    return p.stdout
