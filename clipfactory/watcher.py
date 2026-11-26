"""Stage 1: find new uploads, ended streams and new files, per campaign lane."""
import datetime as dt
import hashlib
import pathlib
import re
import zipfile

import requests

from . import campaigns, config, db, youtube_api
from .util import ffprobe, iso, iso_duration, parse_iso, utcnow, youtube_id

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
REFERENCE_EXT = {".pdf", ".txt", ".md", ".docx", ".doc", ".png", ".jpg", ".jpeg"}


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


def dropbox_direct(url):
    """A Dropbox share link that downloads instead of showing a web page (dl=1). Keeps rlkey and other params."""
    url = url.strip().strip("<>")
    if re.search(r"[?&]dl=\d", url):
        return re.sub(r"([?&])dl=\d", r"\1dl=1", url)
    return url + ("&" if "?" in url else "?") + "dl=1"


def _safe_target(root, name):
    """Where a file from the zip goes, or None if its path tries to leave the folder."""
    parts = [p for p in pathlib.PurePosixPath(name.replace("\\", "/")).parts if p not in ("", ".", "/")]
    if not parts or ".." in parts:
        return None
    target = root.joinpath(*parts).resolve()
    return target if str(target).startswith(str(root.resolve())) else None


def dropbox_download(url, root):
    """Download a Dropbox shared folder (as one zip) or file into `root`; keeps files already there.
    No Dropbox account or app is needed. Returns (new videos, new reference files)."""
    root.mkdir(parents=True, exist_ok=True)
    limit = config.section("watcher").get("dropbox_max_gb", 20) * 1024 ** 3
    tmp = root / ".download.part"
    busy = config.data_dir() / "busy.lock"   # the updater waits while this file is fresh
    busy.write_text(iso(utcnow()))
    try:
        return _download(url, root, limit, tmp, busy)
    finally:
        busy.unlink(missing_ok=True)


def _download(url, root, limit, tmp, busy):
    # read timeout 60 s: a stalled download fails fast and is retried in 2 minutes
    with requests.get(dropbox_direct(url), stream=True, timeout=(20, 60), allow_redirects=True) as r:
        if r.status_code != 200:
            raise RuntimeError(f"Dropbox answered HTTP {r.status_code}; is the link still shared?")
        ctype = r.headers.get("content-type", "")
        if "text/html" in ctype:
            raise RuntimeError("Dropbox showed a web page instead of the files (the folder may be too big to "
                               "download at once, or the link needs a password)")
        name = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', r.headers.get("content-disposition", ""))
        name = requests.utils.unquote(name.group(1)) if name else "download"
        size = 0
        with open(tmp, "wb") as f:
            for n, chunk in enumerate(r.iter_content(1024 * 1024)):
                if n % 200 == 0:
                    busy.write_text(iso(utcnow()))   # still downloading
                size += len(chunk)
                if size > limit:
                    raise RuntimeError("Dropbox folder is bigger than watcher.dropbox_max_gb")
                f.write(chunk)
    videos, refs = [], []
    try:
        if zipfile.is_zipfile(tmp):
            with zipfile.ZipFile(tmp) as z:
                for info in z.infolist():
                    ext = pathlib.PurePosixPath(info.filename).suffix.lower()
                    if info.is_dir() or ext not in VIDEO_EXT | REFERENCE_EXT:
                        continue
                    target = _safe_target(root, info.filename)
                    if not target or (target.exists() and target.stat().st_size == info.file_size):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as src, open(target, "wb") as dst:
                        while True:
                            block = src.read(1024 * 1024)
                            if not block:
                                break
                            dst.write(block)
                    (videos if ext in VIDEO_EXT else refs).append(target)
        else:   # a single shared file
            target = _safe_target(root, name)
            if target and pathlib.Path(name).suffix.lower() in VIDEO_EXT | REFERENCE_EXT and not target.exists():
                tmp.replace(target)
                (videos if target.suffix.lower() in VIDEO_EXT else refs).append(target)
    finally:
        tmp.unlink(missing_ok=True)
    return videos, refs


def _dropbox(c, s, min_dur):
    """A Dropbox share link from the campaign: download it every few hours, then treat it like a folder."""
    root = config.data_dir() / "dropbox" / hashlib.sha1(s["channel_id"].encode()).hexdigest()[:12]
    key = f"dropbox_last:{s['id']}"
    last = db.kv_get(key)
    every = dt.timedelta(hours=config.section("watcher").get("dropbox_every_hours", 6))
    retry_at = db.kv_get(key + ":retry")
    complete = (root / ".complete").exists()   # written only after a whole download finished
    due = not complete or not last or utcnow() - parse_iso(last) >= every
    if due and not (retry_at and utcnow() < parse_iso(retry_at)):
        from . import notify
        if not complete:
            notify.send("progress", f"⬇️ Downloading the Dropbox footage for **{c['name']}**…", campaign_id=c["id"])
        try:
            videos, refs = dropbox_download(s["channel_id"], root)
        except Exception as e:
            # Try again in 15 minutes (not 6 hours): a cut connection or a restart shouldn't stall the campaign.
            minutes = config.section("watcher").get("dropbox_retry_minutes", 2)
            db.kv_set(key + ":retry", iso(utcnow() + dt.timedelta(minutes=minutes)))
            db.kv_set("watcher_soon", iso(utcnow() + dt.timedelta(minutes=minutes)))
            raise RuntimeError(f"Dropbox download failed, trying again in {minutes} min: {e}") from None
        (root / ".complete").write_text(iso(utcnow()))
        db.kv_set(key, iso(utcnow()))
        db.kv_set(key + ":retry", None)
        if videos or refs or not complete:
            db.log(c["id"], f"Dropbox: {len(videos)} new video(s), {len(refs)} reference file(s)")
            notify.send("progress", f"✅ Dropbox for **{c['name']}**: {len(videos)} new video(s) downloaded. "
                        "The AI looks for moments next; clips come here for review.", campaign_id=c["id"])
        _reference_files(c, refs)
    if not root.exists():
        return 0   # nothing downloaded yet; the retry comes soon
    return _folder(c, {**s, "channel_id": str(root)}, min_dur)


def _reference_files(c, refs):
    """Text guides become part of the campaign guide; other files (PDF, images) are listed on Discord."""
    if not refs:
        return
    from . import guide, notify
    texts = [r for r in refs if r.suffix.lower() in (".txt", ".md")]
    for r in texts:
        try:
            guide.add(c["id"], r.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            db.log(c["id"], f"Couldn't read {r.name}: {e}", "warn")
    others = [r for r in refs if r not in texts]
    if others:
        notify.send("progress", f"**{c['name']}**: reference files in the Dropbox folder: "
                    + ", ".join(r.name for r in others[:8])
                    + f"\nOpen them (Clipping Tools\\data\\dropbox) and paste their instructions with "
                    f"`!guide {c['id']} <text>` so the AI follows them.", campaign_id=c["id"])


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
    from . import recipes
    for c in campaigns.active():
        # a campaign format builds from short clips (6-13 s AI Lucia clips): keep them, don't skip "too short"
        fmin = 3 if recipes.get(c) else cfg.get("min_file_duration_s", 15)
        for s in campaigns.sources(c["id"]):
            try:
                if s["kind"] == "twitch_channel":
                    found += _twitch(c, s, cutoff, min_dur)
                elif s["kind"] == "folder":
                    found += _folder(c, s, fmin)
                elif s["kind"] == "gdrive":
                    from . import gdrive
                    found += gdrive.watch(c, s, fmin)
                elif s["kind"] == "dropbox":
                    found += _dropbox(c, s, fmin)
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
