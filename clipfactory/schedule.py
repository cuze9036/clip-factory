"""Pick US prime-time slots for approved clips, per campaign quota and per account caps."""
import datetime as dt
import random

from . import campaigns, config, db
from .util import ET, own_time, iso, parse_iso, utcnow


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


def needs_submit(c):
    """Campaign clips must be submitted on Whop soon after they go live (own-channel lanes have nothing to submit)."""
    return not (c.get("origin") == "yaml" and not c.get("url"))


def awake(when, minutes=0):
    """True when `when` and the next `minutes` fall inside the owner's waking hours (submit.awake_hours, in
    owner_timezone), so a campaign clip never goes live while the owner sleeps and its submit window runs out."""
    hours = config.section("submit").get("awake_hours")
    tz = config.get().get("owner_timezone")
    if not hours or not tz:
        return True
    from zoneinfo import ZoneInfo
    start, end = (dt.time(*map(int, h.split(":"))) for h in hours)

    def inside(t):
        t = t.astimezone(ZoneInfo(tz)).time()
        return start <= t <= end if start <= end else (t >= start or t <= end)
    return inside(when) and inside(when + dt.timedelta(minutes=minutes))


def next_slot(c, platform, account, not_before=None):
    cfg = config.section("schedule")
    cap = cfg.get("max_posts_per_account_per_day", 5)
    gap = dt.timedelta(minutes=cfg.get("min_gap_minutes", 120))
    from . import publish
    manual = publish.mode(platform, account) == "manual"
    slots = ((cfg.get("manual_slots") or {}).get(platform) if manual else None) \
        or (cfg.get("platform_slots") or {}).get(platform)
    offset = dt.timedelta(0) if slots else \
        dt.timedelta(minutes=(cfg.get("platform_offset_min") or {}).get(platform, 0))
    slots = slots or cfg.get("slots", ["12:30"])
    sub = config.section("submit")
    # the tool posts this one by itself and the owner must submit it within minutes: only while they're awake
    guard = not manual and needs_submit(c) and sub.get("awake_hours")
    if guard:
        slots = list(slots) + [s for s in sub.get("awake_slots", []) if s not in slots]   # extra fall-back times
    from . import insights
    weak = insights.skipped_slots(platform)   # slots the weekly review found clearly weaker
    slots = [s for s in slots if s not in weak] or slots
    jitter = cfg.get("slot_jitter_min", 12)   # posts land a few minutes around each slot, like a person posting
    earliest = max(utcnow() + dt.timedelta(minutes=15), not_before or utcnow())
    taken = _taken(platform, account)
    quota_days = _campaign_clip_days(c["id"])
    from . import flops
    daily = flops.quota(c)   # the campaign's quota, plus one a day per recent clip with few views
    day = earliest.astimezone(ET).date()
    for _ in range(30):
        if quota_days.get(day, 0) < daily or not_before:
            same_day = [t for t in taken if t.astimezone(ET).date() == day]
            for hhmm in slots:
                h, m = map(int, hhmm.split(":"))
                when = dt.datetime.combine(day, dt.time(h, m), tzinfo=ET) + offset + dt.timedelta(
                    seconds=random.uniform(-jitter, jitter) * 60)
                if when < earliest or len(same_day) >= cap:
                    continue
                if guard and not awake(when, sub.get("within_minutes", 30)):
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
    db.log(c["id"], f"Clip {clip_id} scheduled for {own_time(first)}")
    return first
