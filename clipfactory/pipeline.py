"""Runs every stage, lane by lane, so one campaign's backlog never blocks another."""
import datetime as dt
import json
import time
import traceback

from . import (analytics, campaigns, config, db, discovery, download, metadata, moments, publish, render,
               review, transcribe, watcher)
from .util import iso, parse_iso, utcnow


def _due(key, minutes):
    last = db.kv_get(key)
    return not last or utcnow() - parse_iso(last) >= dt.timedelta(minutes=minutes)


def stage_analyze(c, limit=1):
    for v in db.q("SELECT * FROM videos WHERE campaign_id=? AND status='new' ORDER BY created_at LIMIT ?", c["id"], limit):
        db.x("UPDATE videos SET status='analyzing' WHERE id=? AND campaign_id=?", v["id"], c["id"])
        try:
            moments.analyze(v, c)
        except Exception as e:
            db.x("UPDATE videos SET status='failed', error=? WHERE id=? AND campaign_id=?", str(e)[:500], v["id"], c["id"])
            db.log(c["id"], f"Finding moments failed for {v['title']}: {e}", "error")


def stage_download(c, limit=2):
    waiting = db.one("""SELECT COUNT(*) n FROM clips WHERE campaign_id=?
                        AND status IN ('downloaded','rendered','pending_review')""", c["id"])["n"]
    room = config.section("download").get("backlog_per_campaign", 6) - waiting
    for m in db.q("SELECT * FROM moments WHERE campaign_id=? AND status='found' ORDER BY score DESC, id LIMIT ?",
                  c["id"], max(0, min(limit, room))):
        v = db.one("SELECT * FROM videos WHERE id=? AND campaign_id=?", m["video_id"], c["id"])
        db.x("UPDATE moments SET status='downloading' WHERE id=?", m["id"])
        try:
            out = download.fetch(v, m["start_s"], m["end_s"], download.moment_path(c["id"], m["id"]))
            db.x("INSERT INTO clips(moment_id, campaign_id, raw_path, created_at) VALUES(?,?,?,?)",
                 m["id"], c["id"], str(out), iso(utcnow()))
            db.x("UPDATE moments SET status='downloaded' WHERE id=?", m["id"])
        except Exception as e:
            db.x("UPDATE moments SET status='failed', error=? WHERE id=?", str(e)[:500], m["id"])
            db.log(c["id"], f"Download failed for moment {m['id']}: {e}", "error")


def _source_layout(v):
    default = config.section("render").get("default_layout", "framed")
    s = db.one("SELECT layout, facecam FROM sources WHERE id=?", v["source_id"]) if v and v["source_id"] else None
    if not s:
        return default, None
    layout = default if s["layout"] in (None, "", "blur", "auto") else s["layout"]
    return layout, json.loads(s["facecam"]) if s["facecam"] else None


def stage_render(c, limit=1):
    for clip in db.q("SELECT * FROM clips WHERE campaign_id=? AND status='downloaded' ORDER BY id LIMIT ?", c["id"], limit):
        m = db.one("SELECT * FROM moments WHERE id=?", clip["moment_id"])
        v = db.one("SELECT * FROM videos WHERE id=? AND campaign_id=?", m["video_id"], c["id"])
        try:
            words = transcribe.words(clip["raw_path"])
            layout, face = _source_layout(v)
            final = clip["raw_path"].replace("_raw.mp4", "_final.mp4")
            res = render.render(clip["raw_path"], final, words, m["hook"], layout, face, credit=c["credit"])
            db.x("UPDATE clips SET final_path=?, duration_s=?, words=?, status='rendered' WHERE id=?",
                 res["path"], res["duration"], json.dumps(words), clip["id"])
        except Exception as e:
            db.x("UPDATE clips SET status='failed', error=? WHERE id=?", str(e)[:500], clip["id"])
            db.log(c["id"], f"Render failed for clip {clip['id']}: {e}", "error")


def stage_metadata(c, limit=3):
    for clip in db.q("SELECT * FROM clips WHERE campaign_id=? AND status='rendered' ORDER BY id LIMIT ?", c["id"], limit):
        m = db.one("SELECT * FROM moments WHERE id=?", clip["moment_id"])
        text = " ".join(w["w"] for w in json.loads(clip["words"] or "[]"))
        meta = metadata.write(text, m, c)
        titles, title, desc, tags = metadata.store_fields(meta)
        db.x("UPDATE clips SET titles=?, title=?, description=?, hashtags=?, status='pending_review' WHERE id=?",
             titles, title, desc, tags, clip["id"])
        if m["score"] >= campaigns.auto_approve_threshold(c):
            review.auto_approve(clip["id"])
        else:
            db.log(c["id"], f"Clip {clip['id']} ready for review: {title}")
            review.notify(clip["id"])


def run_once():
    cfg_w, cfg_s = config.section("watcher"), config.section("stats")
    if _due("watcher_last_run", cfg_w.get("every_minutes", 15)):
        try:
            watcher.check_sources()
        except Exception as e:
            db.log(None, f"Watcher error: {e}", "error")
    for c in campaigns.active():
        for stage in (stage_analyze, stage_download, stage_render, stage_metadata):
            try:
                stage(c)
            except Exception:
                db.log(c["id"], f"{stage.__name__} crashed: {traceback.format_exc(limit=2)}", "error")
    for fn in (publish.run_due,):
        try:
            fn()
        except Exception as e:
            db.log(None, f"{fn.__module__} error: {e}", "error")
    if _due("stats_last_run", cfg_s.get("every_hours", 6) * 60):
        analytics.refresh()
    if _due("discovery_last_run", config.section("discovery").get("every_hours", 6) * 60):
        discovery.run()


def run_forever(pause_s=30):
    db.log(None, "Clip Factory started")
    while True:
        run_once()
        time.sleep(pause_s)
