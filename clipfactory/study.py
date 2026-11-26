"""Daily study: the tool learns from its own clips every day, and keeps what it learned in a folder.

Every day it:
1. reads the numbers: views of every posted clip (YouTube, Instagram, Facebook) and, from YouTube Analytics
   (the owner's own channel, read-only), how long people watched, where they dropped off, where the views came
   from (Shorts feed, search, outside links…) and which countries;
2. puts each clip's numbers next to how it was made (campaign, moment type, edit style, length, title, hook,
   captions, posting time, AI check score);
3. compares with yesterday's snapshot (which clips grew, which stopped);
4. asks the free AI (Gemini / Free LLM API, never Claude) to update the lessons: what to keep doing and what to
   avoid, each with its evidence, keeping lessons that still hold and dropping ones the numbers no longer show;
5. saves everything in data/learning/ (lessons.md to read, lessons.json for the tool, one snapshot a day) and
   adds the lessons to every AI prompt that picks moments, plans edits and writes titles.

YouTube numbers in the snapshots are kept 30 days (YouTube's API rules); the lessons themselves are kept.
"""
import datetime as dt
import json

from . import config, db, llm, notify
from .util import ET, iso, own_time, parse_iso, utcnow

LESSONS = "lessons.json"

PROMPT = """You are the analyst of an automated clipping tool. It cuts short vertical clips (Shorts / Reels) from
long videos for paid clipping campaigns (paid per 1,000 views, US audience) and for the owner's own GTA 6 channel.
Every day you study the results and keep a short list of lessons that the AI which picks moments, plans edits and
writes titles must follow.

Below: the current lessons, today's numbers per clip (with how each clip was made), what changed since the
last snapshot, and channel-level YouTube numbers (traffic sources, countries).

How to judge a clip: views compared with clips of the same platform and similar age; for YouTube also average
percentage viewed and how many viewers were still watching at 25% of the clip (a weak start loses them early).
Only claim what the numbers show. With few clips, keep lessons "low" confidence. Keep a lesson that still holds,
change or drop one the new numbers contradict, add new ones with evidence. At most 10 "do" and 10 "avoid".
Each lesson must be something the AI can act on (a moment type, a hook, a title pattern, an edit style, a clip
length, captions, a posting time), in plain short English.

Return JSON only:
{{"summary": "2-3 sentences for the owner: what happened since yesterday and what was learned",
  "do": [{{"lesson": "...", "evidence": "numbers that show it", "confidence": "low|medium|high"}}],
  "avoid": [{{"lesson": "...", "evidence": "...", "confidence": "low|medium|high"}}],
  "best_clip": "title of today's best clip and why it worked, or empty",
  "worst_clip": "title of the weakest clip and what to change, or empty"}}

Current lessons:
{lessons}

Numbers:
{data}
"""


def folder():
    d = config.data_dir() / "learning"
    (d / "snapshots").mkdir(parents=True, exist_ok=True)
    return d


def _cfg():
    return config.section("study")


# ---------- reading the numbers ----------

def _analytics(account):
    from googleapiclient.discovery import build
    from . import publish
    return build("youtubeAnalytics", "v2", credentials=publish.youtube_creds(account), cache_discovery=False)


def _rows(res):
    cols = [c["name"] for c in res.get("columnHeaders") or []]
    return [dict(zip(cols, r)) for r in res.get("rows") or []]


def youtube_details(posts):
    """{video id: {avg_view_pct, avg_view_s, likes, shares, subs, kept_25pct, sources, countries}} from YouTube
    Analytics (data lags 1-3 days, so new uploads may have none yet)."""
    if not posts:
        return {}, {}
    ya = _analytics(posts[0]["account"])
    start = min(parse_iso(p["posted_at"]) for p in posts).date().isoformat()
    end = utcnow().date().isoformat()
    q = lambda **kw: ya.reports().query(ids="channel==MINE", startDate=start, endDate=end, **kw).execute()  # noqa: E731
    out = {}
    ids = [p["external_id"] for p in posts]
    for i in range(0, len(ids), 50):
        res = q(metrics="views,averageViewPercentage,averageViewDuration,likes,shares,subscribersGained",
                dimensions="video", filters="video==" + ",".join(ids[i:i + 50]), sort="-views", maxResults=50)
        for r in _rows(res):
            out[r["video"]] = {"yt_views": r.get("views"), "avg_view_pct": round(r.get("averageViewPercentage") or 0),
                               "avg_view_s": r.get("averageViewDuration"), "likes": r.get("likes"),
                               "shares": r.get("shares"), "subs_gained": r.get("subscribersGained")}
    for vid in ids[: _cfg().get("detail_videos", 40)]:   # per video: drop-off and where the views came from
        d = out.setdefault(vid, {})
        try:
            ret = _rows(q(metrics="audienceWatchRatio", dimensions="elapsedVideoTimeRatio", filters=f"video=={vid}"))
            near = [r for r in ret if abs(r["elapsedVideoTimeRatio"] - 0.25) <= 0.02]
            if near:
                d["kept_at_25pct"] = round(near[0]["audienceWatchRatio"], 2)
            src = _rows(q(metrics="views", dimensions="insightTrafficSourceType", filters=f"video=={vid}",
                          sort="-views"))
            d["sources"] = {r["insightTrafficSourceType"]: r["views"] for r in src[:5]}
        except Exception as e:
            d["detail_error"] = str(e)[:80]
    channel = {}
    try:
        c_start = (utcnow() - dt.timedelta(days=28)).date().isoformat()
        cq = lambda **kw: ya.reports().query(ids="channel==MINE", startDate=c_start, endDate=end, **kw).execute()  # noqa: E731
        channel["traffic_sources_28d"] = {r["insightTrafficSourceType"]: r["views"] for r in _rows(
            cq(metrics="views", dimensions="insightTrafficSourceType", sort="-views"))[:8]}
        channel["countries_28d"] = {r["country"]: r["views"] for r in _rows(
            cq(metrics="views", dimensions="country", sort="-views", maxResults=8))}
    except Exception as e:
        channel["error"] = str(e)[:120]
    return out, channel


IG_METRICS = ["views", "reach", "likes", "comments", "shares", "saved", "ig_reels_avg_watch_time"]
FB_METRICS = ["blue_reels_play_count", "post_impressions_unique", "post_video_avg_time_watched",
              "post_video_followers"]


def meta_details(clip):
    """Instagram / Facebook numbers of one reel (watch time, reach, shares, saves) from the Graph API."""
    from . import publish
    token = publish.meta_keys(clip["platform"], clip["account"] or "main")[1]
    path = (f"{clip['external_id']}/insights" if clip["platform"] == "instagram"
            else f"{clip['external_id']}/video_insights")
    metrics = IG_METRICS if clip["platform"] == "instagram" else FB_METRICS

    def ask(ms):
        data = publish._g("GET", publish._graph(path), token, params={"metric": ",".join(ms)}, timeout=30)
        out = {}
        for d in data.get("data") or []:
            vals = d.get("values") or []
            v = vals[0].get("value") if vals else (d.get("total_value") or {}).get("value")
            if isinstance(v, (int, float)):
                out[d["name"]] = v
        return out
    try:
        got = ask(metrics)
    except RuntimeError:   # one metric Meta doesn't offer for this reel fails the whole call: ask one by one
        got = {}
        for m in metrics:
            try:
                got.update(ask([m]))
            except RuntimeError:
                pass
    if not got and clip["platform"] == "instagram":   # no insights permission: likes and comments still show
        try:
            m = publish._g("GET", publish._graph(clip["external_id"]), token,
                           params={"fields": "like_count,comments_count"}, timeout=30)
            got = {"likes": m.get("like_count"), "comments": m.get("comments_count"),
                   "note": "views need instagram_manage_insights (run windows\\meta-login.ps1 again)"}
        except RuntimeError as e:
            got = {"detail_error": str(e)[:80]}
    for k in ("ig_reels_avg_watch_time", "post_video_avg_time_watched"):
        if k in got:
            got["avg_watch_s"] = round(got.pop(k) / 1000, 1)
    return got


def clips_table():
    """Every posted clip with its numbers and how it was made."""
    rows = db.q("""SELECT p.id post, p.platform, p.account, p.external_id, p.posted_at, p.scheduled_at, p.views,
                          p.likes, p.us_share, c.name campaign, k.id clip, k.title, k.style, k.duration_s, k.qa,
                          k.plan, m.category, m.hook, m.score
                   FROM posts p JOIN clips k ON k.id=p.clip_id JOIN campaigns c ON c.id=p.campaign_id
                   LEFT JOIN moments m ON m.id=k.moment_id
                   WHERE p.status IN ('posted','uploaded') AND p.posted_at IS NOT NULL AND p.posted_at <= ?
                   ORDER BY p.posted_at DESC LIMIT ?""", iso(utcnow()), _cfg().get("max_clips", 120))
    out = []
    for r in rows:
        t = parse_iso(r["posted_at"])
        try:
            qa = json.loads(r["qa"] or "{}").get("score")
        except ValueError:
            qa = None
        try:
            captions = not (json.loads(r["plan"] or "{}").get("captions") is False)
        except ValueError:
            captions = None
        out.append({"post": r["post"], "platform": r["platform"], "account": r["account"],
                    "external_id": r["external_id"], "posted_at": r["posted_at"],
                    "age_hours": round((utcnow() - t).total_seconds() / 3600), "campaign": r["campaign"],
                    "title": r["title"], "hook": r["hook"], "moment_type": r["category"], "style": r["style"],
                    "length_s": round(r["duration_s"] or 0), "captions": captions, "ai_score": r["score"],
                    "ai_check": qa, "posted_ET": f"{t.astimezone(ET):%a %H:%M}", "views": r["views"] or 0,
                    "likes": r["likes"] or 0, "us_share": r["us_share"]})
    return out


def snapshot():
    clips = clips_table()
    yt = [c for c in clips if c["platform"] == "youtube" and c["external_id"]]
    channel = {}
    if yt:
        try:
            details, channel = youtube_details(yt)
            for c in yt:
                c.update(details.get(c["external_id"], {}))
        except Exception as e:
            channel = {"error": f"YouTube Analytics: {str(e)[:150]}"}
    for c in clips:
        if c["platform"] in ("instagram", "facebook") and c["external_id"]:
            try:
                c.update(meta_details(c))
            except Exception as e:
                c["detail_error"] = str(e)[:80]
    return {"at": iso(utcnow()), "clips": clips, "youtube_channel": channel, "from_screenshots": screenshot_notes()}


def previous():
    snaps = sorted((folder() / "snapshots").glob("*.json"))
    today = utcnow().date().isoformat()
    for f in reversed(snaps):
        if f.stem < today:
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
    return None


def changes(now, before):
    """Views gained per clip since the last snapshot, biggest first."""
    if not before:
        return []
    old = {c["post"]: c["views"] for c in before.get("clips", [])}
    out = [{"post": c["post"], "title": c["title"], "platform": c["platform"], "gained": c["views"] - old[c["post"]]}
           for c in now["clips"] if c["post"] in old]
    return sorted(out, key=lambda x: -x["gained"])[:15]


# ---------- TikTok / X numbers from the owner's screenshots ----------

SHOT_HELP = """📸 **TikTok / X numbers: send screenshots here in #ai-insights, once a day**
1. **TikTok** app → Profile → ☰ → **Creator tools → Analytics → Content**, or open a video → **⋯ → Analytics**.
   **X** app → open your post → **📊 View post analytics**, or your Profile → Posts (views under each post).
2. Take screenshots where the **post text and its views** are visible (likes, watch time, traffic sources too if shown).
3. Send them here, all at once is fine. You can type `tiktok` or `x` with them.
The AI reads them, puts the numbers on the right clips and uses them in the daily study. That's all."""

SHOT_PROMPT = """This is a screenshot from the owner's own social media account (TikTok, X/Twitter, Instagram,
YouTube or Facebook): a post list, one post's analytics, or an account overview. The owner's note: {note!r}.
Read every number you can see. Don't guess numbers that aren't visible. Return JSON only:
{{"platform": "tiktok|x|instagram|youtube|facebook|other",
  "kind": "post list|post analytics|account overview",
  "posts": [{{"text": "the post's caption or title as shown (first 80 characters)", "views": 0, "likes": 0,
             "comments": 0, "shares": 0, "saves": 0, "avg_watch_s": 0, "watched_full_pct": 0,
             "posted": "date as shown or empty", "sources": {{"For You": 0}}}}],
  "account": {{"followers": 0, "views_period": 0, "period": "as shown or empty"}},
  "notes": "anything else useful in one sentence, or empty"}}
Use null for numbers that aren't shown. Views like 12.3K mean 12300."""


def _num(x):
    try:
        return int(float(x)) if x is not None else None
    except (TypeError, ValueError):
        return None


def _validate_shot(d):
    if not isinstance(d, dict) or not isinstance(d.get("posts", []), list):
        raise llm.Invalid("no posts list")
    plat = str(d.get("platform") or "other").lower().strip()
    plat = {"twitter": "x", "twitter/x": "x"}.get(plat, plat)
    posts = []
    for p in d.get("posts") or []:
        if not isinstance(p, dict):
            continue
        posts.append({"text": str(p.get("text") or "")[:120], "views": _num(p.get("views")),
                      "likes": _num(p.get("likes")), "comments": _num(p.get("comments")),
                      "shares": _num(p.get("shares")), "saves": _num(p.get("saves")),
                      "avg_watch_s": p.get("avg_watch_s"), "watched_full_pct": p.get("watched_full_pct"),
                      "posted": str(p.get("posted") or "")[:40],
                      "sources": p.get("sources") if isinstance(p.get("sources"), dict) else {}})
    return {"platform": plat if plat in ("tiktok", "x", "instagram", "youtube", "facebook") else "other",
            "kind": str(d.get("kind") or "")[:40], "posts": posts[:30],
            "account": d.get("account") if isinstance(d.get("account"), dict) else {},
            "notes": str(d.get("notes") or "")[:200]}


def _match(text, posts):
    """Our posted clip whose title / caption is closest to the screenshot's text."""
    import difflib
    best, score = None, 0.0
    t = (text or "").lower()[:80]
    for p in posts:
        for cand in (p["title"], p["description"]):
            cand = (cand or "").lower()[:80]
            if t and cand:
                r = difflib.SequenceMatcher(None, t, cand).ratio()
                if t[:25] in cand or cand[:25] in t:
                    r = max(r, 0.9)
                if r > score:
                    best, score = p, r
    return best if score >= 0.45 else (posts[0] if len(posts) == 1 and not t else None)


def read_screenshot(path, note=""):
    """One screenshot → numbers saved on the matching clips. Returns a short reply for Discord."""
    import os
    st = os.stat(path)
    shot, _ = llm.complete_json("screenshot", SHOT_PROMPT.format(note=(note or "")[:200]), _validate_shot,
                                video={"image": str(path), "stamp": f"{st.st_size}:{st.st_mtime_ns}"})
    note_l = (note or "").lower().strip()
    plat = shot["platform"]
    if plat == "other":
        plat = "tiktok" if "tiktok" in note_l else "x" if ("twitter" in note_l or note_l in ("x", "x post")) else plat
    ours = db.q("""SELECT p.id, k.title, k.description FROM posts p JOIN clips k ON k.id=p.clip_id
                   WHERE p.platform=? AND p.status='posted' ORDER BY p.posted_at DESC LIMIT 60""", plat)
    matched, unmatched = [], []
    for s in shot["posts"]:
        m = _match(s["text"], ours) if ours else None
        if m and s["views"] is not None:
            db.x("UPDATE posts SET views=MAX(COALESCE(views,0), ?), likes=MAX(COALESCE(likes,0), ?), stats_at=? "
                 "WHERE id=?", s["views"], s["likes"] or 0, iso(utcnow()), m["id"])
            matched.append(f"post {m['id']}: {s['views']:,} views")
        else:
            unmatched.append(s["text"][:40] or "?")
    notes = db.kv_get("screenshot_notes") or []
    notes.append({"at": iso(utcnow()), "platform": plat, **{k: shot[k] for k in ("kind", "posts", "account", "notes")}})
    db.kv_set("screenshot_notes", notes[-40:])
    out = f"📸 Read a {plat if plat != 'other' else 'social'} screenshot: {len(shot['posts'])} post(s)"
    if matched:
        out += " · saved " + ", ".join(matched[:6])
    if unmatched:
        out += f" · {len(unmatched)} not matched to our clips (kept for the study anyway)"
    return out + "."


def screenshot_notes(days=14):
    cutoff = iso(utcnow() - dt.timedelta(days=days))
    return [n for n in db.kv_get("screenshot_notes") or [] if n.get("at", "") >= cutoff]


# ---------- lessons ----------

def lessons():
    f = folder() / LESSONS
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"do": [], "avoid": [], "updated": None, "days": 0}


def _items(d, key):
    out = []
    for x in (d.get(key) or [])[:10]:
        if isinstance(x, str):
            x = {"lesson": x}
        if not isinstance(x, dict) or not str(x.get("lesson") or "").strip():
            continue
        conf = str(x.get("confidence") or "low").lower()
        out.append({"lesson": str(x["lesson"]).strip()[:200], "evidence": str(x.get("evidence") or "").strip()[:200],
                    "confidence": conf if conf in ("low", "medium", "high") else "low"})
    return out


def _validate(d):
    if not isinstance(d, dict) or not str(d.get("summary") or "").strip():
        raise llm.Invalid("no summary")
    out = {"summary": str(d["summary"]).strip()[:700], "do": _items(d, "do"), "avoid": _items(d, "avoid"),
           "best_clip": str(d.get("best_clip") or "").strip()[:300],
           "worst_clip": str(d.get("worst_clip") or "").strip()[:300]}
    llm.require_english(out["summary"], " ".join(x["lesson"] for x in out["do"] + out["avoid"]))
    return out


def write_lessons(ai, data_at):
    old = lessons()
    new = {"do": ai["do"], "avoid": ai["avoid"], "updated": data_at, "days": (old.get("days") or 0) + 1,
           "summary": ai["summary"], "best_clip": ai["best_clip"], "worst_clip": ai["worst_clip"]}
    d = folder()
    (d / LESSONS).write_text(json.dumps(new, indent=2, ensure_ascii=False), encoding="utf-8")
    md = [f"# What our clips taught us (updated {own_time(parse_iso(data_at))}, day {new['days']})", "",
          new["summary"], "", "## Keep doing"]
    md += [f"- {x['lesson']} ({x['confidence']}; {x['evidence']})" for x in new["do"]] or ["- nothing proven yet"]
    md += ["", "## Avoid"]
    md += [f"- {x['lesson']} ({x['confidence']}; {x['evidence']})" for x in new["avoid"]] or ["- nothing proven yet"]
    if new["best_clip"]:
        md += ["", f"Best clip: {new['best_clip']}"]
    if new["worst_clip"]:
        md += [f"Weakest clip: {new['worst_clip']}"]
    (d / "lessons.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    with open(d / "history.md", "a", encoding="utf-8") as f:   # one line per day: how the learning went
        f.write(f"- {data_at[:10]}: {new['summary']}\n")
    return new


def block():
    """The lessons, for every AI prompt that picks moments, plans edits or writes titles."""
    ls = lessons()
    lines = [f"Do: {x['lesson']}" for x in ls.get("do", []) if x.get("confidence") != "low"][:8] + \
            [f"Avoid: {x['lesson']}" for x in ls.get("avoid", []) if x.get("confidence") != "low"][:8]
    lines += [f"Maybe ({x['confidence']}): {x['lesson']}" for x in ls.get("do", []) if x.get("confidence") == "low"][:3]
    return ("\n\nLessons from our own clips' views (daily study, follow them):\n" + "\n".join(f"- {x}" for x in lines)
            if lines else "")


def prune(days=30):
    """YouTube's API rules: stored YouTube numbers older than 30 days are deleted (the lessons stay)."""
    cutoff = (utcnow() - dt.timedelta(days=days)).date().isoformat()
    gone = 0
    for f in (folder() / "snapshots").glob("*.json"):
        if f.stem < cutoff:
            f.unlink()
            gone += 1
    return gone


# ---------- the daily run ----------

def run(send=True):
    from . import analytics
    try:
        analytics.refresh()   # fresh view counts first
    except Exception as e:
        db.log(None, f"Daily study: view refresh failed ({str(e)[:120]})", "warn")
    now = snapshot()
    before = previous()
    d = folder()
    (d / "snapshots" / f"{utcnow().date().isoformat()}.json").write_text(
        json.dumps(now, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    prune()
    db.kv_set("study_last_run", iso(utcnow()))
    if send and not db.kv_get("study_help_sent"):   # once: how to send TikTok / X numbers
        notify.send("insights", SHOT_HELP)
        db.kv_set("study_help_sent", True)
    with_views = [c for c in now["clips"] if c["views"]]
    if len(with_views) < _cfg().get("min_clips", 3):
        msg = (f"📚 **Daily study** · {len(now['clips'])} posted clip(s), {len(with_views)} with views so far. "
               "Not enough to learn from yet; numbers saved, I'll compare tomorrow.")
        if send:
            notify.send("insights", msg)
        return msg
    data = {"clips": [{k: v for k, v in c.items() if k not in ("external_id", "account")} for c in now["clips"]],
            "since_last_snapshot": changes(now, before), "youtube_channel": now["youtube_channel"],
            "tiktok_x_from_screenshots": now.get("from_screenshots") or []}
    old = lessons()
    try:
        ai, provider = llm.complete_json("study", PROMPT.format(
            lessons=json.dumps({"do": old.get("do", []), "avoid": old.get("avoid", [])}, ensure_ascii=False),
            data=json.dumps(data, default=str, ensure_ascii=False)[:24000]), _validate)
    except llm.LLMError as e:
        db.log(None, f"Daily study: the AI didn't answer ({str(e)[:150]})", "warn")
        return "📚 Daily study: numbers saved, but the AI didn't answer; I'll try again tomorrow."
    new = write_lessons(ai, now["at"])
    msg = report(new, now, before)
    if send:
        notify.send("insights", msg)
    db.log(None, f"Daily study done ({len(now['clips'])} clips, {len(new['do'])} do / {len(new['avoid'])} avoid)")
    return msg


def report(new, now, before):
    total = sum(c["views"] for c in now["clips"])
    gained = sum(x["gained"] for x in changes(now, before)) if before else None
    out = [f"📚 **Daily study** · {len(now['clips'])} clips · {total:,} views"
           + (f" (+{gained:,} since yesterday)" if gained is not None else ""), new["summary"]]
    src = (now.get("youtube_channel") or {}).get("traffic_sources_28d") or {}
    if src:
        out.append("YouTube views come from: " + ", ".join(f"{k.replace('_', ' ').lower()} {v:,}"
                                                          for k, v in list(src.items())[:4]))
    if new["do"]:
        out.append("**Keep doing**\n" + "\n".join(f"✅ {x['lesson']}" for x in new["do"][:5]))
    if new["avoid"]:
        out.append("**Avoid**\n" + "\n".join(f"⚠️ {x['lesson']}" for x in new["avoid"][:5]))
    out.append("Full notes: data\\learning\\lessons.md (the AI uses them for every new clip). "
               "TikTok / X: send today's screenshots here (`!study help` shows how).")
    return "\n".join(out)[:1900]
