"""Clips that got few views: the lane makes more and better ones instead of giving up.

A clip "flopped" when, `after_hours` after posting, its best platform still has fewer views than `views_below`
(or the campaign's minimum views to get paid, whichever is higher). For each flop:
- the campaign may post one extra clip a day (up to double its daily quota), so fresh moments go out sooner;
- unless the campaign's guide forbids reposts, the same moment is made once more as a new version: a different
  edit style, a tighter start and new, stronger titles.
TikTok and X views aren't read yet, so only YouTube, Instagram and Facebook count.
"""
import datetime as dt

from . import campaigns, config, db, notify
from .util import iso, parse_iso, utcnow

NO_REPOST = ("repost", "re-post", "duplicate", "same clip", "reupload", "re-upload")


def _cfg():
    return config.section("flops")


def threshold(c):
    return max(int(_cfg().get("views_below", 300)), int(c.get("min_views") or 0))


def flopped(c, days=4):
    """Clip ids of this campaign that flopped in the last few days."""
    cfg = _cfg()
    now = utcnow()
    old = now - dt.timedelta(hours=cfg.get("after_hours", 48))
    since = now - dt.timedelta(days=days)
    best = {}
    for p in db.q("""SELECT clip_id, views, posted_at, stats_at FROM posts WHERE campaign_id=? AND status='posted'
                     AND platform IN ('youtube','instagram','facebook')""", c["id"]):
        if not p["posted_at"] or not p["stats_at"]:
            continue
        t = parse_iso(p["posted_at"])
        if t > old or t < since:
            continue
        best[p["clip_id"]] = max(best.get(p["clip_id"], 0), p["views"] or 0)
    return [cid for cid, v in best.items() if v < threshold(c)]


def quota(c):
    """Clips a day for this campaign: the normal quota plus one per recent flop (at most double)."""
    base = c.get("daily_clip_quota") or 3
    if campaigns.boosting(c):   # until it has earned something, post as much as the boost allows
        base = max(base, config.section("boost").get("daily_clip_quota", 6))
    if not _cfg().get("enabled", True):
        return base
    return base + min(base, len(flopped(c)))


def reposts_allowed(c):
    from . import guide
    text = " ".join(guide.get(c)["avoid"] + list(c.get("rules") or [])).lower()
    return not any(w in text for w in NO_REPOST)


def remake(clip_id):
    """A new version of a flopped clip's moment (once per moment). Returns the new moment id or None."""
    clip = db.one("SELECT * FROM clips WHERE id=?", clip_id)
    m = db.one("SELECT * FROM moments WHERE id=?", clip["moment_id"]) if clip else None
    if not m or db.kv_get(f"remade:{m['id']}"):
        return None
    db.kv_set(f"remade:{m['id']}", True)
    start = (m["start_s"] or 0) + 1.0 if (m["end_s"] or 0) - (m["start_s"] or 0) > 12 else m["start_s"]
    cur = db.conn().execute(
        """INSERT INTO moments(video_id, campaign_id, start_s, end_s, score, category, hook, why, created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (m["video_id"], m["campaign_id"], start, m["end_s"], max(1.0, (m["score"] or 7) - 1), m["category"],
        m["hook"], f"New version of clip #{clip_id}, which got few views", iso(utcnow())))
    new_id = cur.lastrowid
    db.kv_set(f"remade:{new_id}", True)   # a remake isn't remade again
    used = list(db.kv_get(f"styles_used:{m['id']}") or [])
    if clip.get("style"):
        used.append(clip["style"])
    db.kv_set(f"styles_used:{new_id}", used[-5:])   # the new version gets a style the old one didn't have
    db.kv_set(f"title_hint:{new_id}", "The first version of this clip got few views. Write clearly stronger, more "
              "curious titles with a different angle than: " + (clip.get("title") or ""))
    return new_id


def run():
    """Look for flops in every running campaign (after each views refresh)."""
    if not _cfg().get("enabled", True):
        return
    for c in campaigns.active():
        fresh = [cid for cid in flopped(c) if not db.kv_get(f"flop_seen:{cid}")]
        if not fresh:
            continue
        remade = []
        for cid in fresh:
            db.kv_set(f"flop_seen:{cid}", iso(utcnow()))
            if _cfg().get("remake", True) and reposts_allowed(c) and remake(cid):
                remade.append(cid)
        db.kv_set("watcher_now", True)
        msg = [f"📉 **{c['name']}**: {len(fresh)} clip(s) got under {threshold(c):,} views in "
               f"{_cfg().get('after_hours', 48)} h (#{', #'.join(map(str, fresh))})."]
        if remade:
            msg.append(f"Making a new version of {len(remade)} of them: different style, tighter start, new titles.")
        elif not reposts_allowed(c):
            msg.append("This campaign doesn't allow reposts, so no new versions.")
        msg.append(f"More fresh clips go out meanwhile: up to {quota(c)} a day for now.")
        notify.send("progress", "\n".join(msg), campaign_id=c["id"])
        db.log(c["id"], f"Flops: {fresh}; remade {remade}")
