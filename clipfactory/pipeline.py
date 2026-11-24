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
    from . import check
    if not db.one("SELECT 1 FROM videos WHERE campaign_id=? AND status='new'", c["id"]):
        return
    if not check.ready(c):   # read the whole campaign first; only then watch its videos
        return
    for v in db.q("SELECT * FROM videos WHERE campaign_id=? AND status='new' ORDER BY created_at LIMIT ?", c["id"], limit):
        db.x("UPDATE videos SET status='analyzing' WHERE id=? AND campaign_id=?", v["id"], c["id"])
        try:
            if config.section("analyst").get("enabled", True):
                try:   # understand the whole video first, then plan the clips
                    from . import analyst
                    analyst.analyze(v, c)
                except Exception as e:
                    db.log(c["id"], f"Full video analysis failed, using the quick moment finder: {str(e)[:200]}", "warn")
                    moments.analyze(v, c)
            else:
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


def director_plan(m, words):
    """The edit the director planned for this moment (None = the automatic edit)."""
    try:
        p = json.loads(m.get("plan") or "null")
    except (TypeError, ValueError):
        return None
    if not isinstance(p, dict):
        return None
    wanted = set(p.pop("emphasis_words", []) or [])
    p["emphasis"] = [i for i, w in enumerate(words)
                     if "".join(ch for ch in w["w"].lower() if ch.isalpha()) in wanted][:6]
    if not p.get("hook") and m.get("hook"):
        p["hook"] = {"text": m["hook"], "until": config.section("effects").get("hook_seconds", 1.8)}
    return p


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


def find_facecam(c, v, raw_path):
    """The webcam box of this video (found once per video, then remembered), or None."""
    key = f"facecam:{c['id']}:{v['id'] if v else raw_path}"
    known = db.kv_get(key)
    if known is None:
        from . import effects
        known = effects.find_facecam(raw_path) or []
        db.kv_set(key, known)
        if known:
            db.log(c["id"], f"{(v or {}).get('title', 'This video')}: webcam found, clips use the split layout")
    return known or None


def pick_style(c, m, layout, face, clip, captions=True, vertical=False):
    """The edit style: a campaign's fixed choice, else one that fits the content (varied for the weekly review).
    'framed' / 'blur' set in config.yaml keep the older looks."""
    fixed = config.section("render").get("style", "auto")
    if fixed in ("framed", "legacy"):
        return None
    db.kv_set(f"restyle:{clip['id']}", None)
    key = f"styles_used:{m['id']}"   # every remake / re-edit of this moment gets a style it hasn't had yet
    used = db.kv_get(key) or []
    if not captions and vertical:   # already 9:16 with its own captions: bold_crop is the whole frame, the boxed
        db.kv_set(key, (used + ["bold_crop"])[-5:])   # styles would crop its top, bottom and captions away
        return "bold_crop"
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
            lang = (v or {}).get("language")
            if not lang:   # never guess English: Hindi speech through the English model gave nonsense captions
                lang = transcribe.language(clip["raw_path"])
                if v:
                    db.x("UPDATE videos SET language=? WHERE id=? AND campaign_id=?", lang, v["id"], c["id"])
            words = transcribe.words(clip["raw_path"], translate=lang != "en")
            layout, face = _source_layout(v)
            if not face:   # a streamer's webcam in the footage: webcam on top, game below (never zoom into it)
                face = find_facecam(c, v, clip["raw_path"])
                if face:
                    layout = "facecam_split"
            final = clip["raw_path"].replace("_raw.mp4", "_final.mp4")
            captions = want_captions(c, v, clip["raw_path"]) and not db.kv_get(f"captions_off:{m['id']}")
            vertical = False
            if not captions:
                src = render.ffprobe(clip["raw_path"])
                vertical = src["width"] / max(1, src["height"]) < 0.7
            style = pick_style(c, m, layout, face, clip, captions, vertical)
            res = render.render(clip["raw_path"], final, words, m["hook"], layout, face, credit=c["credit"],
                                style=style, accent=styles.accent_for(c["topic"], m["category"] or ""),
                                captions=captions, plan=director_plan(m, words))
            db.x("UPDATE clips SET final_path=?, duration_s=?, words=?, style=?, plan=?, status='rendered' WHERE id=?",
                 res["path"], res["duration"], json.dumps(words), style,
                 json.dumps(res.get("plan"), ensure_ascii=False) if res.get("plan") else None, clip["id"])
        except Exception as e:
            db.x("UPDATE clips SET status='failed', error=? WHERE id=?", str(e)[:500], clip["id"])
            db.log(c["id"], f"Render failed for clip {clip['id']}: {e}", "error")
            continue
        check_clip(c, {**clip, "style": style}, res, captions)


def check_clip(c, clip, res, captions):
    """The AI watches the finished clip; a clip that fails is fixed by itself (another cut, style or no captions)."""
    from . import qa
    if not config.section("qa").get("enabled", True):
        return
    try:
        r = qa.check(res["path"], res["duration"], c, clip["style"], captions, res.get("plan"), c["credit"])
    except Exception as e:   # the check is a safety net: an AI outage never stops clips
        db.log(c["id"], f"AI check of clip {clip['id']} skipped: {str(e)[:150]}", "warn")
        return
    qa.store(clip["id"], r)
    if qa.passed(r):
        return
    problems = ", ".join(sorted({i["type"].replace("_", " ") for i in r["issues"]})) or "weak"
    done = qa.fix(clip, r)
    if done:
        db.log(c["id"], f"Clip {clip['id']} AI check {r['score']:.0f}/10 ({problems}): {done}")
    else:
        db.log(c["id"], f"Clip {clip['id']} AI check {r['score']:.0f}/10 ({problems}): goes to review as it is", "warn")


def stage_metadata(c, limit=3):
    for clip in db.q("SELECT * FROM clips WHERE campaign_id=? AND status='rendered' ORDER BY id LIMIT ?", c["id"], limit):
        m = db.one("SELECT * FROM moments WHERE id=?", clip["moment_id"])
        text = " ".join(w["w"] for w in json.loads(clip["words"] or "[]"))
        from . import reedit
        meta = metadata.write(text, m, c, hint=reedit.consume_hint(m["id"]))
        titles, title, desc, tags = metadata.store_fields(meta)
        db.x("UPDATE clips SET titles=?, title=?, description=?, hashtags=?, status='pending_review' WHERE id=?",
             titles, title, desc, tags, clip["id"])
        from . import qa
        if m["score"] >= campaigns.auto_approve_threshold(c) and qa.passed(qa.get(clip)):
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
        try:
            _keep_up(c)
        except Exception as e:
            db.log(c["id"], f"Campaign upkeep failed: {str(e)[:200]}", "warn")
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
        try:
            from . import flops
            flops.run()   # clips with few views: new versions and more fresh clips
        except Exception as e:
            db.log(None, f"Flop check failed: {str(e)[:200]}", "warn")
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


def _keep_up(c):
    """Per campaign, now and then: the campaign check (also for lanes that ran before it existed) and a fresh read
    of its Whop page (new brief, footage or budget)."""
    from . import check, whop_page
    if check.needed(c) and not check.get(c) and _due(f"check_try:{c['id']}", 60):
        db.kv_set(f"check_try:{c['id']}", iso(utcnow()))
        check.ready(c)
    if campaigns._extra(c).get("whop_url") and _due(f"whop_read:{c['id']}", 6 * 60):
        db.kv_set(f"whop_read:{c['id']}", iso(utcnow()))
        whop_page.refresh(c)


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
