"""Stage 1: find new uploads, ended streams and new files, per campaign lane."""
import datetime as dt
import hashlib
import pathlib

from . import campaigns, config, db, youtube_api
from .util import ffprobe, iso, iso_duration, parse_iso, utcnow, youtube_id

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def _insert(c, s, vid, title, url=None, local_path=None, is_stream=0, duration=None, published=None, status="new",
            settle=None):
    return db.x("""INSERT OR IGNORE INTO videos(id, campaign_id, source_id, title, url, local_path, is_stream,
                   duration_s, published_at, status, settle, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                vid, c["id"], s["id"] if s else None, title, url, local_path, is_stream, duration, published, status,
                settle, iso(utcnow()))


def _youtube(c, s, cutoff, min_dur):
    channel = youtube_api.resolve_channel(s["channel_id"])
    ids = youtube_api.recent_uploads(channel)
    known = {r["id"] for r in db.q("SELECT id FROM videos WHERE campaign_id=?", c["id"])}
    fresh = [i for i in ids if i not in known]
    found = 0
    for v in youtube_api.videos(fresh) if fresh else []:
        sn, live = v["snippet"], v.get("liveStreamingDetails")
        if sn.get("liveBroadcastContent") in ("live", "upcoming") or (live and not live.get("actualEndTime")):
            continue  # still streaming; picked up once it ends
        if s["streams_only"] and not live:
            continue
        if parse_iso(sn["publishedAt"]) < cutoff or v.get("status", {}).get("privacyStatus") != "public":
            continue
        dur = iso_duration(v["contentDetails"].get("duration"))
        if not dur or dur < min_dur:
            continue
        _insert(c, s, v["id"], sn["title"], url=f"https://www.youtube.com/watch?v={v['id']}", is_stream=int(bool(live)),
                duration=dur, published=sn["publishedAt"])
        db.log(c["id"], f"{'Stream ended' if live else 'New upload'}: {sn['title']} ({dur // 60} min)")
        found += 1
    return found


def _twitch(c, s, cutoff, min_dur):
    """Twitch VODs appear while the stream is still live, so a VOD waits until its length stops growing."""
    from yt_dlp import YoutubeDL
    url = f"https://www.twitch.tv/{s['channel_id']}/videos?filter=archives&sort=time"
    with YoutubeDL({"quiet": True, "extract_flat": True, "playlistend": 8}) as y:
        entries = (y.extract_info(url, download=False) or {}).get("entries") or []
    found = 0
    for e in entries:
        vid = "twitch-" + str(e.get("id", "")).lstrip("v")
        dur = e.get("duration") or 0
        row = db.one("SELECT status, settle FROM videos WHERE id=? AND campaign_id=?", vid, c["id"])
        if row and row["status"] != "waiting":
            continue
        if e.get("timestamp") and dt.datetime.fromtimestamp(e["timestamp"], dt.timezone.utc) < cutoff:
            continue
        if not row:
            _insert(c, s, vid, e.get("title") or vid, url=e.get("url") or f"https://www.twitch.tv/videos/{vid[7:]}",
                    is_stream=1, duration=dur, status="waiting", settle=str(dur))
        elif row["settle"] == str(dur) and dur >= min_dur:
            db.x("UPDATE videos SET status='new', duration_s=? WHERE id=? AND campaign_id=?", dur, vid, c["id"])
            db.log(c["id"], f"Stream ended: {e.get('title')} ({int(dur) // 60} min)")
            found += 1
        else:
            db.x("UPDATE videos SET settle=?, duration_s=? WHERE id=? AND campaign_id=?", str(dur), dur, vid, c["id"])
    return found


def _folder(c, s, min_dur):
    """A folder of raw files from the campaign, e.g. a synced Google Drive or Dropbox folder.
    A file waits until its size stops changing, so half-synced files are never clipped."""
    root = pathlib.Path(s["channel_id"]).expanduser()
    if not root.is_dir():
        raise RuntimeError(f"Folder not found: {root}")
    found = 0
    for f in sorted(root.rglob("*")):
        if f.suffix.lower() not in VIDEO_EXT or not f.is_file():
            continue
        vid = "file-" + hashlib.sha1(str(f.resolve()).encode()).hexdigest()[:12]
        size = str(f.stat().st_size)
        row = db.one("SELECT status, settle FROM videos WHERE id=? AND campaign_id=?", vid, c["id"])
        if row and row["status"] != "waiting":
            continue
        if not row:
            _insert(c, s, vid, f.stem, local_path=str(f.resolve()), status="waiting", settle=size)
        elif row["settle"] == size:
            dur = ffprobe(f)["duration"]
            if dur < min_dur:
                db.x("UPDATE videos SET status='skipped', error='too short' WHERE id=? AND campaign_id=?", vid, c["id"])
                continue
            db.x("UPDATE videos SET status='new', duration_s=? WHERE id=? AND campaign_id=?", dur, vid, c["id"])
            db.log(c["id"], f"New file: {f.name} ({int(dur) // 60} min)")
            found += 1
        else:
            db.x("UPDATE videos SET settle=? WHERE id=? AND campaign_id=?", size, vid, c["id"])
    return found


def check_sources():
    cfg = config.section("watcher")
    cutoff = utcnow() - dt.timedelta(days=cfg.get("lookback_days", 3))
    min_dur = cfg.get("min_duration_s", 120)
    found = 0
    for c in campaigns.active():
        for s in campaigns.sources(c["id"]):
            try:
                if s["kind"] == "twitch_channel":
                    found += _twitch(c, s, cutoff, min_dur)
                elif s["kind"] == "folder":
                    found += _folder(c, s, min_dur)
                else:
                    found += _youtube(c, s, cutoff, min_dur)
                db.x("UPDATE sources SET last_checked=? WHERE id=?", iso(utcnow()), s["id"])
            except Exception as e:
                db.log(c["id"], f"Watcher failed for {s['name']}: {e}", "error")
    db.kv_set("watcher_last_run", iso(utcnow()))
    return found


def add_video(ref, campaign_id):
    """Add one video link (YouTube, Twitch, Kick or anything yt-dlp supports) or a local file to a lane."""
    if not campaigns.get(campaign_id):
        raise SystemExit(f"Unknown campaign {campaign_id!r}. Run: clipfactory sync")
    p = pathlib.Path(ref).expanduser()
    if p.exists():
        info = ffprobe(p)
        vid = "local-" + p.stem[:40]
        db.x("""INSERT OR REPLACE INTO videos(id, campaign_id, title, local_path, duration_s, status, created_at)
                VALUES(?,?,?,?,?, 'new', ?)""", vid, campaign_id, p.stem, str(p.resolve()), info["duration"],
             iso(utcnow()))
        db.log(campaign_id, f"Added local video {p.name}")
        return vid
    yid = youtube_id(ref)
    title = dur = None
    is_stream = 0
    if yid:
        url = f"https://www.youtube.com/watch?v={yid}"
        try:
            v = youtube_api.videos([yid])[0]
            title, dur = v["snippet"]["title"], iso_duration(v["contentDetails"]["duration"])
            is_stream = int("liveStreamingDetails" in v)
        except Exception:
            pass
    else:
        url = ref
    if title is None:
        from yt_dlp import YoutubeDL
        with YoutubeDL({"quiet": True, "skip_download": True}) as y:
            info = y.extract_info(url, download=False)
        title, dur = info.get("title"), info.get("duration")
        is_stream = int(bool(info.get("was_live") or "twitch.tv/videos" in url))
        yid = yid or f"{info.get('extractor_key', 'web').lower()}-{info.get('id')}"
    db.x("""INSERT OR REPLACE INTO videos(id, campaign_id, title, url, is_stream, duration_s, status, created_at)
            VALUES(?,?,?,?,?,?, 'new', ?)""", yid, campaign_id, title, url, is_stream, dur, iso(utcnow()))
    db.log(campaign_id, f"Added video: {title}")
    return yid
