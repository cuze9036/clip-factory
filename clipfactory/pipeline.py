"""Runs every stage, lane by lane, so one campaign's backlog never blocks another."""
import datetime as dt
import json
import random
import time
import traceback

from . import (analytics, campaigns, config, db, discovery, download, metadata, moments, publish, render,
               review, styles, transcribe, watcher)
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


def want_captions(c, v, raw_path):
    """Our captions or not: the campaign's setting (💬 on/off), else auto: off when the footage already has
    captions burned in (checked once per video)."""
    setting = campaigns._extra(c).get("captions", "auto")
    if setting in ("on", "off"):
        return setting == "on"
    key = f"burned_captions:{c['id']}:{v['id'] if v else raw_path}"
    known = db.kv_get(key)
    if known is None:
        from . import effects
        known = effects.has_burned_captions(raw_path)
        db.kv_set(key, known)
        if known:
            db.log(c["id"], f"{(v or {}).get('title', 'This video')} already has captions, so ours are left out")
    return not known


def pick_style(c, m, layout, face, clip, captions=True):
    """The edit style: a campaign's fixed choice, else one that fits the content (varied for the weekly review).
    'framed' / 'blur' set in config.yaml keep the older looks."""
    fixed = config.section("render").get("style", "auto")
    if fixed in ("framed", "legacy"):
        return None
    db.kv_set(f"restyle:{clip['id']}", None)
    key = f"styles_used:{m['id']}"   # every remake / re-edit of this moment gets a style it hasn't had yet
    used = db.kv_get(key) or []
    if not captions:   # footage with its own captions: show the whole frame so their text isn't cropped off
        avoid = list(used) + ["bold_crop", "word_pop", "split"]
        style = styles.choose(c["topic"], m["category"] or "", avoid=avoid)
        if style in ("bold_crop", "word_pop", "split"):
            style = "blur_box"
        db.kv_set(key, (used + [style])[-5:])
        return style
    style = styles.choose(c["topic"], m["category"] or "", facecam=bool(layout == "facecam_split" and face),
                          avoid=used)
    db.kv_set(key, (used + [style])[-5:])
    return style


def stage_render(c, limit=1):
    for clip in db.q("SELECT * FROM clips WHERE campaign_id=? AND status='downloaded' ORDER BY id LIMIT ?", c["id"], limit):
        m = db.one("SELECT * FROM moments WHERE id=?", clip["moment_id"])
        v = db.one("SELECT * FROM videos WHERE id=? AND campaign_id=?", m["video_id"], c["id"])
        try:
            words = transcribe.words(clip["raw_path"])
            layout, face = _source_layout(v)
            final = clip["raw_path"].replace("_raw.mp4", "_final.mp4")
            captions = want_captions(c, v, clip["raw_path"])
            style = pick_style(c, m, layout, face, clip, captions)
            res = render.render(clip["raw_path"], final, words, m["hook"], layout, face, credit=c["credit"],
                                style=style, accent=styles.accent_for(c["topic"], m["category"] or ""),
                                captions=captions)
            db.x("UPDATE clips SET final_path=?, duration_s=?, words=?, style=?, plan=?, status='rendered' WHERE id=?",
                 res["path"], res["duration"], json.dumps(words), style,
                 json.dumps(res.get("plan"), ensure_ascii=False) if res.get("plan") else None, clip["id"])
        except Exception as e:
            db.x("UPDATE clips SET status='failed', error=? WHERE id=?", str(e)[:500], clip["id"])
            db.log(c["id"], f"Render failed for clip {clip['id']}: {e}", "error")


def stage_metadata(c, limit=3):
    for clip in db.q("SELECT * FROM clips WHERE campaign_id=? AND status='rendered' ORDER BY id LIMIT ?", c["id"], limit):
        m = db.one("SELECT * FROM moments WHERE id=?", clip["moment_id"])
        text = " ".join(w["w"] for w in json.loads(clip["words"] or "[]"))
        from . import reedit
        meta = metadata.write(text, m, c, hint=reedit.consume_hint(m["id"]))
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
    soon = db.kv_get("watcher_soon")   # a retry that is due (e.g. a Dropbox download), before the next regular check
    if soon and utcnow() >= parse_iso(soon):
        db.kv_set("watcher_soon", None)
        db.kv_set("watcher_now", True)
    if db.kv_get("watcher_now") or _due("watcher_last_run", cfg_w.get("every_minutes", 5) * random.uniform(0.8, 1.2)):
        db.kv_set("watcher_now", None)   # set when a source is added or a campaign starts
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
    gw = config.section("gmail_watch")
    if gw.get("enabled", True) and _due("gmail_last_run", gw.get("every_minutes", 30)):
        db.kv_set("gmail_last_run", iso(utcnow()))
        try:
            from . import gmail_watch
            gmail_watch.check()
        except Exception as e:
            db.log(None, f"Gmail audit watcher failed: {str(e)[:200]}", "warn")
    ins = config.section("insights")
    if ins.get("autofix", True) and _due("autofix_last_run", ins.get("autofix_every_minutes", 5)):
        try:
            from . import autofix
            autofix.run()
        except Exception as e:
            db.log(None, f"Auto-fix failed: {str(e)[:200]}", "warn")
    if ins.get("enabled", True) and _due("health_last_run", ins.get("daily_check_hours", 24) * 60):
        try:
            from . import health
            health.run()
        except Exception as e:
            db.log(None, f"Daily check failed: {str(e)[:200]}", "warn")
    if ins.get("enabled", True):
        if not db.kv_get("insights_last_run"):
            db.kv_set("insights_last_run", iso(utcnow()))   # first week starts now
        elif _due("insights_last_run", ins.get("every_days", 7) * 24 * 60):
            try:
                from . import insights
                insights.run()
            except Exception as e:
                db.log(None, f"Weekly review failed: {str(e)[:200]}", "warn")
    if _due("discovery_last_run", config.section("discovery").get("every_hours", 6) * 60):
        discovery.run()


def resume_interrupted():
    """Work cut off by a restart (update, crash, laptop off) starts again right away instead of hanging."""
    v = db.conn().execute("UPDATE videos SET status='new' WHERE status='analyzing'").rowcount
    m = db.conn().execute("UPDATE moments SET status='found' WHERE status='downloading'").rowcount
    if v or m:
        db.log(None, f"Resumed work cut off by a restart: {v} video(s), {m} moment(s)")


def run_forever(pause_s=30):
    db.log(None, "Clip Factory started")
    resume_interrupted()
    while True:
        run_once()
        time.sleep(pause_s)
