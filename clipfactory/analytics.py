"""Stage 8: views, likes and US viewer share for every posted clip."""
import datetime as dt

import requests

from . import config, db, publish, youtube_api
from .util import iso, utcnow


def _youtube(posts):
    ids = [p["external_id"] for p in posts]
    try:
        stats = youtube_api.stats(ids)
    except Exception:
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", credentials=publish.youtube_creds(posts[0]["account"]), cache_discovery=False)
        stats = {v["id"]: v["statistics"] for v in yt.videos().list(part="statistics", id=",".join(ids)).execute()["items"]}
    for p in posts:
        s = stats.get(p["external_id"], {})
        db.x("UPDATE posts SET views=?, likes=?, stats_at=? WHERE id=?", int(s.get("viewCount", p["views"] or 0)),
             int(s.get("likeCount", p["likes"] or 0)), iso(utcnow()), p["id"])
        try:
            db.x("UPDATE posts SET us_share=? WHERE id=?", _us_share(p), p["id"])
        except Exception:
            pass


def _us_share(p):
    from googleapiclient.discovery import build
    ya = build("youtubeAnalytics", "v2", credentials=publish.youtube_creds(p["account"]), cache_discovery=False)
    start = (utcnow() - dt.timedelta(days=60)).date().isoformat()
    rows = ya.reports().query(ids="channel==MINE", startDate=start, endDate=utcnow().date().isoformat(),
                              metrics="views", dimensions="country", filters=f"video=={p['external_id']}").execute()
    rows = rows.get("rows") or []
    total = sum(r[1] for r in rows)
    return round(sum(r[1] for r in rows if r[0] == "US") / total, 3) if total else None


def _graph_views(p, token, metrics):
    for metric in metrics:
        r = requests.get(publish._graph(f"{p['external_id']}/insights" if p["platform"] == "instagram"
                                        else f"{p['external_id']}/video_insights"),
                         params={"metric": metric, "access_token": token}, timeout=30)
        if r.ok and r.json().get("data"):
            return int(r.json()["data"][0]["values"][0]["value"])
    return None


def refresh():
    since = iso(utcnow() - dt.timedelta(days=45))
    posts = db.q("SELECT * FROM posts WHERE status IN ('posted','uploaded') AND external_id IS NOT NULL "
                 "AND posted_at >= ?", since)
    # YouTube stats are refreshed for every upload, not only recent ones: Google's policy says stored
    # API data must be refreshed at least every 30 days, and purge_stale() deletes what could not be.
    yt = db.q("SELECT * FROM posts WHERE platform='youtube' AND status IN ('posted','uploaded') "
              "AND external_id IS NOT NULL")
    for i in range(0, len(yt), 50):
        try:
            _youtube(yt[i:i + 50])
        except Exception as e:
            db.log(None, f"YouTube stats failed: {e}", "warn")
    for p in posts:
        if p["platform"] == "youtube":
            continue
        try:
            acc = publish._account(p["platform"], p["account"])
            token = config.env(acc["token_env"])
            metrics = ["views", "plays"] if p["platform"] == "instagram" else [
                "fb_reels_total_plays", "blue_reels_play_count", "total_video_views"]
            v = _graph_views(p, token, metrics)
            if v is not None:
                db.x("UPDATE posts SET views=?, stats_at=? WHERE id=?", v, iso(utcnow()), p["id"])
        except Exception as e:
            db.log(p["campaign_id"], f"{p['platform']} stats failed: {e}", "warn")
    purge_stale()
    db.kv_set("stats_last_run", iso(utcnow()))
    return len(posts)


def purge_stale(days=30):
    """Delete YouTube statistics that have not been refreshed for `days` days (e.g. after access was removed)."""
    cutoff = iso(utcnow() - dt.timedelta(days=days))
    return db.conn().execute("UPDATE posts SET views=0, likes=0, us_share=NULL, stats_at=NULL "
                             "WHERE platform='youtube' AND stats_at IS NOT NULL AND stats_at < ?", (cutoff,)).rowcount
