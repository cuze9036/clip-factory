"""Weekly self-review: the tool looks at its own results, asks the free AI (Gemini / Free LLM API, never Claude)
what is working and what isn't, applies the safe improvements itself and sends you the rest on Discord.

What it looks at (last 7 days): views and likes per platform, per posting time, per moment type and per campaign,
the best and worst clips, whether the AI score predicts views, clips you rejected and why, posts you didn't make
in time, and errors.

What it changes by itself (small, reversible, shown in the report):
- "What has worked" tips for titles and moment picking, added to every AI prompt.
- A posting slot that clearly underperforms (5+ posts, under half the platform's average views) is skipped.
  Never more than one slot per platform, and every platform keeps at least two.

What it only suggests (you decide): AI score thresholds, clip length, quotas, which campaigns to drop.
"""
import datetime as dt
import json
from collections import defaultdict

from . import config, db, llm, notify
from .util import ET, iso, parse_iso, utcnow

TIPS_KEY = "insights_tips"
SKIP_KEY = "insights_skip_slots"

PROMPT = """You review the results of an automated clipping tool. It cuts short vertical clips from long videos
for paid clipping campaigns (paid per 1,000 views, US audience) and posts them on YouTube Shorts, Instagram Reels,
Facebook Reels, TikTok and X. Below are last week's numbers.

Find what is working and what is not, and give concrete, short advice. Only claim what the numbers show; if there
is too little data for something, say so. Plain, simple English.

Return JSON only:
{{"summary": "2-3 sentences: how the week went",
  "working": ["up to 5 short points"],
  "not_working": ["up to 5 short points"],
  "title_tips": ["up to 4 rules for clip titles, based on the best and worst titles"],
  "moment_tips": ["up to 4 rules for which moments to pick, based on categories and views"],
  "suggestions": ["up to 5 changes for the owner to consider (thresholds, clip length, campaigns, posting)"],
  "problems": ["up to 5 problems to fix (errors, missed posts, rejections)"]}}

Numbers:
{data}
"""


def _lines(d, key, n):
    return [str(x).strip()[:160] for x in d.get(key) or [] if str(x).strip()][:n]


def _validate(d):
    if not isinstance(d, dict) or not str(d.get("summary") or "").strip():
        raise llm.Invalid("no summary")
    out = {"summary": str(d["summary"]).strip()[:600]}
    for k, n in (("working", 5), ("not_working", 5), ("title_tips", 4), ("moment_tips", 4), ("suggestions", 5),
                 ("problems", 5)):
        out[k] = _lines(d, k, n)
    llm.require_english(out["summary"], " ".join(out["title_tips"] + out["moment_tips"]))
    return out


# ---------- numbers ----------

def _slot(t, platform):
    """The configured slot a post time belongs to (posts land up to ~12 min around it)."""
    cfg = config.section("schedule")
    slots = ((cfg.get("platform_slots") or {}).get(platform) or []) + ((cfg.get("manual_slots") or {}).get(platform) or [])
    et = t.astimezone(ET)
    best, gap = None, None
    for hm in set(slots):
        h, m = map(int, hm.split(":"))
        d = abs((et.hour * 60 + et.minute) - (h * 60 + m))
        if gap is None or d < gap:
            best, gap = hm, d
    return best if gap is not None and gap <= 45 else f"{et:%H}:00"


def _avg(xs):
    return round(sum(xs) / len(xs)) if xs else 0


def collect(days=7):
    since = iso(utcnow() - dt.timedelta(days=days))
    posts = db.q("""SELECT p.id, p.platform, p.scheduled_at, p.posted_at, p.views, p.likes, p.status, p.mode,
                           c.name campaign, k.title, m.category, m.score, m.hook
                    FROM posts p JOIN clips k ON k.id=p.clip_id JOIN campaigns c ON c.id=p.campaign_id
                    LEFT JOIN moments m ON m.id=k.moment_id
                    WHERE p.scheduled_at >= ? AND p.scheduled_at <= ?""", since, iso(utcnow()))
    done = [p for p in posts if p["status"] in ("posted", "uploaded") and p["posted_at"]]
    by = {k: defaultdict(list) for k in ("platform", "slot", "category", "campaign", "score")}
    for p in done:
        v = p["views"] or 0
        by["platform"][p["platform"]].append(v)
        by["slot"][f"{p['platform']} {_slot(parse_iso(p['scheduled_at']), p['platform'])}"].append(v)
        by["category"][p["category"] or "unknown"].append(v)
        by["campaign"][p["campaign"]].append(v)
        by["score"]["AI score 9-10" if (p["score"] or 0) >= 9 else "AI score below 9"].append(v)
    table = {k: {name: {"posts": len(vs), "avg_views": _avg(vs)} for name, vs in g.items()} for k, g in by.items()}
    ranked = sorted(done, key=lambda p: -(p["views"] or 0))
    clip = lambda p: {"title": p["title"], "platform": p["platform"], "views": p["views"] or 0,  # noqa: E731
                      "category": p["category"], "hook": p["hook"]}
    missed = [p for p in posts if p["status"] == "manual" and parse_iso(p["scheduled_at"]) < utcnow() - dt.timedelta(hours=2)]
    rejected = db.q("SELECT reject_reason FROM clips WHERE status='rejected' AND reviewed_at >= ?", since)
    reasons = defaultdict(int)
    for r in rejected:
        reasons[(r["reject_reason"] or "no reason")[:60]] += 1
    errors = defaultdict(int)
    for e in db.q("SELECT msg FROM events WHERE level='error' AND at >= ?", since):
        errors[e["msg"].split(":")[0][:80]] += 1
    flow = {k: db.one(sql, since)["n"] for k, sql in {
        "videos_found": "SELECT COUNT(*) n FROM videos WHERE created_at >= ?",
        "moments_found": "SELECT COUNT(*) n FROM moments WHERE created_at >= ?",
        "clips_made": "SELECT COUNT(*) n FROM clips WHERE created_at >= ?",
    }.items()}
    return {
        "days": days, "posts_made": len(done), "posts_scheduled": len(posts),
        "total_views": sum(p["views"] or 0 for p in done),
        "by_platform": table["platform"], "by_posting_slot_ET": table["slot"], "by_moment_type": table["category"],
        "by_campaign": table["campaign"], "ai_score_vs_views": table["score"],
        "best_clips": [clip(p) for p in ranked[:5]], "worst_clips": [clip(p) for p in ranked[-5:][::-1]] if len(ranked) > 5 else [],
        "hand_posts_not_made": len(missed), "rejected_clips": dict(reasons), "errors": dict(errors), "pipeline": flow,
    }


# ---------- changes it makes itself ----------

def slot_skips(data):
    """{platform: [slot]} for slots with 5+ posts and under half the platform's average views."""
    out = {}
    plat_avg = {p: s["avg_views"] for p, s in data["by_platform"].items()}
    cfg = config.section("schedule")
    for key, s in data["by_posting_slot_ET"].items():
        platform, slot = key.split(" ", 1)
        if s["posts"] < 5 or not plat_avg.get(platform) or s["avg_views"] >= plat_avg[platform] / 2:
            continue
        slots = set((cfg.get("platform_slots") or {}).get(platform) or []) | set((cfg.get("manual_slots") or {}).get(platform) or [])
        if slot in slots and len(slots) > 2 and platform not in out:
            out[platform] = [slot]
    return out


def skipped_slots(platform):
    return (db.kv_get(SKIP_KEY) or {}).get(platform, [])


def tips_block():
    """'What has worked on our channels' lines for the AI prompts."""
    t = db.kv_get(TIPS_KEY) or {}
    lines = [f"Title: {x}" for x in t.get("titles", [])] + [f"Moments: {x}" for x in t.get("moments", [])]
    return ("\n\nWhat has worked on our channels so far (from last week's views):\n"
            + "\n".join(f"- {x}" for x in lines)) if lines else ""


# ---------- the weekly run ----------

def enough(data):
    return data["posts_made"] >= config.section("insights").get("min_posts", 5)


def run(days=7, send=True):
    data = collect(days)
    changes, ai = [], None
    if enough(data):
        try:
            ai, provider = llm.complete_json("insights", PROMPT.format(data=json.dumps(data, default=str)[:20000]),
                                             _validate)
        except llm.LLMError as e:
            db.log(None, f"Weekly review: the AI didn't answer ({str(e)[:150]})", "warn")
        if ai and (ai["title_tips"] or ai["moment_tips"]):
            db.kv_set(TIPS_KEY, {"titles": ai["title_tips"], "moments": ai["moment_tips"],
                                 "at": iso(utcnow())})
            changes.append("Updated the 'what has worked' tips the AI uses for titles and moments")
        skips = slot_skips(data)
        if skips != (db.kv_get(SKIP_KEY) or {}):
            db.kv_set(SKIP_KEY, skips)
            if skips:
                changes.append("Skipping weak posting slots: " + "; ".join(f"{p} {', '.join(s)} ET" for p, s in skips.items()))
            else:
                changes.append("Using every posting slot again")
    db.kv_set("insights_last_run", iso(utcnow()))
    report = format_report(data, ai, changes)
    if send:
        for part in _parts(report):
            notify.send("insights", part)
    db.log(None, f"Weekly review done ({data['posts_made']} posts, {data['total_views']:,} views)")
    return report


def _parts(text, size=1800):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > size:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    return parts + [cur] if cur.strip() else parts


def format_report(data, ai, changes):
    now = dt.datetime.now(ET)
    out = [f"📊 **Weekly review** · {now:%b %d} · last {data['days']} days",
           f"{data['posts_made']} posts made · {data['total_views']:,} views · "
           f"{data['pipeline']['videos_found']} videos found · {data['pipeline']['moments_found']} moments · "
           f"{data['pipeline']['clips_made']} clips made"]
    if data["by_platform"]:
        out.append("By platform: " + " · ".join(f"{p} {s['avg_views']:,} avg ({s['posts']})"
                                               for p, s in sorted(data["by_platform"].items(), key=lambda x: -x[1]["avg_views"])))
    if not enough(data):
        out.append(f"Not enough posts yet for real advice (needs {config.section('insights').get('min_posts', 5)}+ "
                   "with views). I'll look again next week.")
    if ai:
        out.append(f"\n{ai['summary']}")
        for title, key, icon in (("Working", "working", "✅"), ("Not working", "not_working", "⚠️"),
                                 ("Suggestions for you", "suggestions", "💡"), ("Problems to fix", "problems", "🔧")):
            if ai[key]:
                out.append(f"**{title}**")
                out += [f"{icon} {x}" for x in ai[key]]
    if data["hand_posts_not_made"]:
        out.append(f"🔧 {data['hand_posts_not_made']} hand-post(s) were not made in time (see `!todo`).")
    if data["errors"]:
        out.append("Errors this week: " + "; ".join(f"{m} ×{n}" for m, n in sorted(data["errors"].items(), key=lambda x: -x[1])[:5]))
    if changes:
        out.append("**Changed by itself**")
        out += [f"⚙️ {c}" for c in changes]
    return "\n".join(out)
