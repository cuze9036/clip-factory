"""Pick US prime-time slots for approved clips, per campaign quota and per account caps."""
import datetime as dt

from . import campaigns, config, db
from .util import ET, iso, parse_iso, utcnow


def _taken(platform, account):
    return [parse_iso(r["scheduled_at"]) for r in db.q(
        "SELECT scheduled_at FROM posts WHERE platform=? AND account=? AND status != 'failed' AND scheduled_at IS NOT NULL",
        platform, account)]


def _campaign_clip_days(campaign_id):
    days = {}
    for r in db.q("""SELECT DISTINCT clip_id, MIN(scheduled_at) at FROM posts WHERE campaign_id=? AND status != 'failed'
                     GROUP BY clip_id""", campaign_id):
        d = parse_iso(r["at"]).astimezone(ET).date()
        days[d] = days.get(d, 0) + 1
    return days


def next_slot(c, platform, account, not_before=None):
    cfg = config.section("schedule")
    cap = cfg.get("max_posts_per_account_per_day", 5)
    gap = dt.timedelta(minutes=cfg.get("min_gap_minutes", 120))
    offset = dt.timedelta(minutes=(cfg.get("platform_offset_min") or {}).get(platform, 0))
    earliest = max(utcnow() + dt.timedelta(minutes=15), not_before or utcnow())
    taken = _taken(platform, account)
    quota_days = _campaign_clip_days(c["id"])
    day = earliest.astimezone(ET).date()
    for _ in range(30):
        if quota_days.get(day, 0) < c["daily_clip_quota"] or not_before:
            same_day = [t for t in taken if t.astimezone(ET).date() == day]
            for hhmm in cfg.get("slots", ["12:30"]):
                h, m = map(int, hhmm.split(":"))
                when = dt.datetime.combine(day, dt.time(h, m), tzinfo=ET) + offset
                if when < earliest or len(same_day) >= cap:
                    continue
                if any(abs(when - t) < gap for t in taken):
                    continue
                return when.astimezone(dt.timezone.utc)
        day += dt.timedelta(days=1)
    raise RuntimeError(f"No free slot in 30 days for {platform}/{account}")


def schedule_clip(clip_id):
    clip = db.one("SELECT * FROM clips WHERE id=?", clip_id)
    c = campaigns.get(clip["campaign_id"])
    from . import publish
    # Instagram/Facebook without keys are skipped; YouTube always stays, so a missing login shows up as an error.
    platforms = [p for p in c["platforms"] if p == "youtube" or publish.platform_ready(p, c["accounts"].get(p, "main"))]
    skipped = [p for p in c["platforms"] if p not in platforms]
    if skipped:
        db.log(c["id"], f"Clip {clip_id}: not posting to {', '.join(skipped)} (no keys set yet)", "warn")
    platforms = platforms or ["youtube"]
    first = None
    for platform in platforms:
        account = c["accounts"].get(platform, "main")
        if first is None:
            when = next_slot(c, platform, account)
            first = when
        else:
            when = next_slot(c, platform, account, not_before=first - dt.timedelta(minutes=1))
        db.x("INSERT INTO posts(clip_id, campaign_id, platform, account, scheduled_at, mode) VALUES(?,?,?,?,?,?)",
             clip_id, c["id"], platform, account, iso(when), publish.mode(platform, account) or "auto")
    db.x("UPDATE clips SET status='scheduled' WHERE id=?", clip_id)
    db.log(c["id"], f"Clip {clip_id} scheduled for {first.astimezone(ET):%a %b %d, %I:%M %p} ET")
    return first
