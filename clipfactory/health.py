"""Daily check for #ai-insights: the tool looks for problems in its own work and says what to do about them.

It never changes anything; the weekly review (insights.py) does the learning. Checks:
- errors in the last 24 hours, grouped
- work that got stuck: videos being analysed for hours, clips not finished, posts that didn't go out
- hand-posts you didn't make, clips waiting for review too long
- campaigns: no new footage for days, budget almost gone, ending soon
"""
import datetime as dt
from collections import defaultdict

from . import campaigns, db, notify
from .util import ET, own_time, iso, parse_iso, utcnow


def _ago(hours):
    return iso(utcnow() - dt.timedelta(hours=hours))


def problems():
    out = []
    errors = defaultdict(int)
    for e in db.q("SELECT msg FROM events WHERE level='error' AND at >= ?", _ago(24)):
        errors[e["msg"].split(":")[0][:80]] += 1
    for msg, n in sorted(errors.items(), key=lambda x: -x[1])[:5]:
        out.append(("🔧", f"Error ×{n}: {msg}", "Check `/selftest`; if it keeps happening, send it to Claude Code."))

    n = db.one("SELECT COUNT(*) n FROM videos WHERE status='analyzing' AND created_at < ?", _ago(3))["n"]
    if n:
        out.append(("⏳", f"{n} video(s) have been analysing for over 3 hours", "Type `!retry`."))
    n = db.one("SELECT COUNT(*) n FROM clips WHERE status IN ('downloaded','rendered') AND created_at < ?", _ago(6))["n"]
    if n:
        out.append(("⏳", f"{n} clip(s) not finished after 6 hours", "Type `!retry`."))
    n = db.one("""SELECT COUNT(*) n FROM posts WHERE status='queued' AND mode='auto' AND platform != 'youtube'
                  AND scheduled_at < ?""", _ago(1))["n"]
    if n:
        out.append(("⚠️", f"{n} automatic post(s) didn't go out on time", "Check `/selftest` (Instagram/Facebook keys)."))
    n = db.one("SELECT COUNT(*) n FROM posts WHERE status='manual' AND scheduled_at < ?", _ago(2))["n"]
    if n:
        out.append(("📱", f"{n} hand-post(s) not made yet", "See `!todo`; post them or tap Skip."))
    n = db.one("SELECT COUNT(*) n FROM clips WHERE status='pending_review' AND created_at < ?", _ago(24))["n"]
    if n:
        out.append(("👀", f"{n} clip(s) waiting for your review for over a day", "See `!review`."))
    n = db.one("""SELECT COUNT(*) n FROM posts WHERE status='posted' AND submitted=0 AND posted_at < ?
                  AND campaign_id IN (SELECT id FROM campaigns WHERE url != '' AND rate_per_1k > 0)""", _ago(1))["n"]
    if n:
        out.append(("💸", f"{n} post(s) not submitted on the campaign page", "See `!todo` and submit them, then `!submitted`."))

    today = dt.datetime.now(ET).date()
    for c in campaigns.active():
        if c.get("origin") == "yaml":
            continue
        last = db.one("SELECT MAX(created_at) t FROM videos WHERE campaign_id=?", c["id"])["t"]
        if last and parse_iso(last) < utcnow() - dt.timedelta(days=3):
            out.append(("🎬", f"{c['name']}: no new footage for 3+ days", "Tap 🎬 Footage on its card and add a source."))
        elif not last and not campaigns.sources(c["id"]):
            out.append(("🎬", f"{c['name']}: no footage yet", "Tap 🎬 Footage on its card in #active-campaigns."))
        if c["budget_left"] is not None and 0 < c["budget_left"] < 500:
            out.append(("💰", f"{c['name']}: only ${c['budget_left']:,.0f} budget left", "Pick the next campaign in #campaign-updates."))
        if c["ends"]:
            left = (dt.date.fromisoformat(c["ends"]) - today).days
            if 0 <= left <= 2:
                out.append(("📅", f"{c['name']}: ends in {left} day(s)", "Pick the next campaign in #campaign-updates."))
    return out


def report():
    found = problems()
    now = dt.datetime.now(ET)
    posted = db.one("SELECT COUNT(*) n FROM posts WHERE status IN ('posted','uploaded') AND posted_at >= ?", _ago(24))["n"]
    made = db.one("SELECT COUNT(*) n FROM clips WHERE created_at >= ?", _ago(24))["n"]
    head = f"🩺 **Daily check** · {own_time(now)} · last 24 h: {made} clips made, {posted} posts out"
    from . import autofix
    fixed = autofix.recent_log(24)
    tail = ("\n🛠️ **Fixed by itself (24 h)**\n" + "\n".join(f"• {t}" for t in fixed[-8:])) if fixed else ""
    if not found:
        return head + "\n✅ Everything looks fine." + tail
    return head + "\n" + "\n".join(f"{icon} {what}\n    → {fix}" for icon, what, fix in found) + tail


def run():
    text = report()
    db.kv_set("health_last_run", iso(utcnow()))
    notify.send("health", text[:1900])
    return text
