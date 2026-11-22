"""From "which campaigns are live?" to a running lane, with you doing only the Whop part.

1. `!campaign` (no text) or `!live` posts every live campaign as a card: numbers, an "Open campaign" link,
   "I joined" and "Not for me" buttons.
2. You join it on Whop yourself (the tool never touches your Whop account) and tap "I joined".
3. You paste the campaign page into Discord (no command needed). The AI reads the rules, hashtags, credit and
   footage links, adds the sources and starts the lane. From there everything is automatic.

Campaigns come from the Whop finder (Apify) and from #campaign-feed, a channel where the announcement channels of
clipping communities you follow are posted.
"""
import datetime as dt
import json
import re
from urllib.parse import quote_plus

from . import campaigns, config, db, notify
from .util import iso, parse_iso, utcnow

AWAIT_KEY = "awaiting_brief"
AWAIT_MINUTES = 60
FEED_HINTS = re.compile(r"(\$\s?\d|\bcpm\b|per\s*1\s*k\b|per\s*1,?000|/\s*1k\b|\brewards?\b|\bcampaign\b)", re.I)
DRIVE = re.compile(r"https?://drive\.google\.com/\S+", re.I)
DROPBOX = re.compile(r"https?://(?:www\.)?dropbox\.com/(?:scl/f[io]|sh|s)/\S+", re.I)


# ---------- which campaigns are live ----------

def _live_hours():
    return max(3, 3 * config.section("discovery").get("every_hours", 1))


def is_live(c):
    """Seen in a recent finder run (or posted in #campaign-feed in the last 3 days), with budget left."""
    if c.get("stage") != "suggested":
        return False
    if c["budget_left"] is not None and c["budget_left"] <= 0:
        return False
    if c["ends"] and c["ends"] < dt.date.today().isoformat():
        return False
    now = utcnow()
    if c.get("seen_at") and now - parse_iso(c["seen_at"]) <= dt.timedelta(hours=_live_hours()):
        return True
    return c.get("origin") == "feed" and bool(c.get("found_at")) and \
        now - parse_iso(c["found_at"]) <= dt.timedelta(days=3)


def _rank(c):
    ok, _ = campaigns.fit(c)
    clippers = campaigns._extra(c).get("clippers") or 0
    budget = c["budget_left"] or 0
    per_clipper = budget / max(clippers, 1)
    return (not ok, not c["rate_per_1k"], -c["rate_per_1k"], -per_clipper)


def live_campaigns():
    return sorted([c for c in campaigns.all_campaigns() if is_live(c)], key=_rank)


def open_link(c):
    """The campaign's own page, or a search that finds it on Whop."""
    if c["url"] and c["url"].startswith("http"):
        return c["url"]
    return "https://www.google.com/search?q=" + quote_plus(f'site:whop.com "{c["name"]}"')


def buttons(c):
    return [["Open campaign", open_link(c), "link"], ["I joined", f"join:{c['id']}", "green"],
            ["Not for me", f"ign:{c['id']}", "grey"]]


def card(c, n=None):
    from .commands import _money, _short_name, _where
    ok, reasons = campaigns.fit(c)
    clippers = campaigns._extra(c).get("clippers") or 0
    bits = [f"${c['rate_per_1k']:.2f} per 1K views" if c["rate_per_1k"] else "pay: see the campaign page"]
    if c["budget_left"] is not None:
        bits.append(f"{_money(c['budget_left'])} left")
    if clippers:
        bits.append(f"{clippers:,} clippers")
    if c["max_payout_per_clip"]:
        bits.append(f"max {_money(c['max_payout_per_clip'])} per clip")
    head = f"**{f'{n}. ' if n else ''}{_short_name(c['name'])}**" + (" · weak fit" if not ok else "")
    lines = [head, " · ".join(bits), f"Posts to: {_where(c)}"]
    if c.get("origin") == "feed":
        lines.append("Found in #campaign-feed")
    if reasons:
        lines.append("Heads up: " + "; ".join(reasons[:3]))
    return "\n".join(lines)


def post_live(limit=8):
    """Reply for `!campaign` / `!live`: a header now, then one card per campaign (with buttons) from the outbox."""
    if not db.one("SELECT 1 FROM campaigns WHERE seen_at IS NOT NULL"):
        from . import discovery   # first time after the update: check Whop now so the list isn't empty
        try:
            discovery.run()
        except Exception as e:
            db.log(None, f"Campaign finder failed: {e}", "warn")
    live = live_campaigns()
    running = [c for c in campaigns.all_campaigns() if campaigns.lane_state(c) == "running"]
    if not live:
        return ("No live campaigns found right now. The finder checks every few hours, and #campaign-feed is read "
                "as soon as something is posted there. Type `!find` to check now.")
    db.kv_set("suggested_order", [c["id"] for c in live])
    for n, c in enumerate(live[:limit], 1):
        notify.send("campaign", card(c, n), campaign_id=c["id"], buttons=buttons(c))
    more = f" (showing the best {limit}; `!suggested` lists all)" if len(live) > limit else ""
    run = f"\nRunning now: {', '.join(c['name'] for c in running)}" if running else ""
    return (f"**{len(live)} live campaign(s)**, best first{more}.\n"
            "Tap **Open campaign**, join it on Whop, then tap **I joined**." + run)


# ---------- after you join ----------

def joined(cid):
    c = campaigns.get(cid)
    if not c:
        return f"No campaign `{cid}`."
    extra = campaigns._extra(c)
    extra["joined_at"] = iso(utcnow())
    db.x("UPDATE campaigns SET notes=? WHERE id=?", json.dumps(extra), cid)
    expect(cid, "brief", AWAIT_MINUTES)
    db.log(cid, "You joined this campaign")
    return (f"Nice, you joined **{c['name']}**.\n"
            "Last step: on the Whop campaign page press **Ctrl+A**, then **Ctrl+C**, and paste it here "
            "(no command needed; a long paste turns into a file, that's fine).\n"
            "I'll read the rules, hashtags, credit and footage links, and start the lane.")


def awaiting():
    """The campaign waiting for something you paste (its page, a guide or a footage link), or None."""
    a = db.kv_get(AWAIT_KEY)
    if not a or utcnow() > parse_iso(a["until"]):
        return None
    return a["cid"]


def awaiting_kind():
    """'brief' (after I joined), 'guide' or 'source' (after the card buttons)."""
    a = db.kv_get(AWAIT_KEY)
    return (a or {}).get("kind", "brief") if awaiting() else None


def expect(cid, kind, minutes=30):
    """The next thing you paste (in #general or a campaign board) goes to this campaign as `kind`."""
    db.kv_set(AWAIT_KEY, {"cid": cid, "kind": kind, "until": iso(utcnow() + dt.timedelta(minutes=minutes))})


def done_waiting():
    db.kv_set(AWAIT_KEY, None)


def last_joined():
    """The campaign you most recently tapped "I joined" on, or None."""
    best = None
    for c in campaigns.all_campaigns():
        j = campaigns._extra(c).get("joined_at")
        if j and (not best or j > best[0]):
            best = (j, c["id"])
    return best[1] if best else None


def add_link(cid, line):
    """A link or folder for a campaign you joined: add it as footage and start the lane. Returns a reply."""
    c = campaigns.get(cid)
    line = line.strip().strip("<>")
    if "whop.com" in line:
        return (f"That's the campaign page on Whop. I can't log in there, so open it, press **Ctrl+A**, **Ctrl+C** "
                f"and paste the text here. Or send the creator's YouTube/Twitch channel: `!source {cid} <link>`.")
    if DRIVE.match(line):
        return ("That's a Google Drive folder. Sync it with Google Drive for Desktop, then send "
                f"`!source {cid} \"G:\\My Drive\\<folder name>\"`.")
    try:
        reply = campaigns.add_source(cid, line)
    except ValueError as e:
        return f"{e}"
    if c and c.get("stage") == "suggested" and campaigns._extra(c).get("joined_at"):
        campaigns.activate(cid)
        db.kv_set(AWAIT_KEY, None)
        reply += f"\n**{c['name']} is running.** Clips will show up here for review."
    elif c and c.get("stage") == "suggested":
        reply += f"\nWhen you've joined it on Whop, start it with `!activate {cid}`."
    return reply


def apply_brief(cid, text):
    """Read the pasted campaign page into an existing campaign, add its footage and start the lane."""
    c = campaigns.get(cid)
    if not c:
        return f"No campaign `{cid}`."
    data, _ = campaigns.extract_from_brief(text)
    sets = {}
    for f in ("rate_per_1k", "budget_left", "max_payout_per_clip", "min_views", "ends", "credit", "topic"):
        if data.get(f) not in (None, ""):
            sets[f] = data[f]
    for f in ("platforms", "required_hashtags", "rules"):
        if data.get(f):
            sets[f] = json.dumps(data[f])
    lo_hi = data.get("clip_length")
    if isinstance(lo_hi, list) and len(lo_hi) == 2 and all(isinstance(x, (int, float)) for x in lo_hi):
        sets["min_len"], sets["max_len"] = lo_hi
    if data.get("url") and str(data["url"]).startswith("http") and not c["url"]:
        sets["url"] = data["url"]
    sets["brief"] = text[:20000]
    db.x(f"UPDATE campaigns SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", *sets.values(), cid)

    added, problems = [], []
    for line in data.get("sources") or []:
        line = str(line).strip()
        if DRIVE.match(line):
            continue
        try:
            added.append(campaigns.add_source(cid, line))
        except (ValueError, SystemExit, Exception) as e:
            problems.append(f"{line[:80]} ({str(e)[:80]})")
    for link in sorted(set(m.group(0).rstrip(').,') for m in DROPBOX.finditer(text))):
        if not db.one("SELECT 1 FROM sources WHERE campaign_id=? AND channel_id=?", cid, link):
            try:
                added.append(campaigns.add_source(cid, link))
            except (ValueError, Exception) as e:
                problems.append(f"{link[:80]} ({str(e)[:80]})")
    drive = sorted(set(m.group(0).rstrip(').,') for m in DRIVE.finditer(text)))
    try:   # the page's instructions become the campaign guide the AI follows
        from . import guide
        guide.add(cid, text)
        g = guide.get(campaigns.get(cid))
        added.append(f"Guide saved: {len(g['do'])} things to do, {len(g['avoid'])} to avoid (see `!guide {cid}`)")
    except Exception as e:
        db.log(cid, f"Couldn't make a guide from the page: {str(e)[:150]}", "warn")
    db.kv_set(AWAIT_KEY, None)

    c = campaigns.get(cid)
    has_content = campaigns.sources(cid) or db.one("SELECT 1 FROM videos WHERE campaign_id=?", cid) \
        or campaigns._extra(c).get("videos")
    out = [f"Read the campaign page for **{c['name']}**."]
    rules = c["rules"][:5]
    if rules:
        out.append("Rules the AI will follow: " + "; ".join(rules))
    if c["required_hashtags"] or c["credit"]:
        out.append("Every caption gets: " + " ".join(c["required_hashtags"] + ([c["credit"]] if c["credit"] else [])))
    out += [f"• {a}" for a in added]
    if drive:
        out.append("The footage is on Google Drive. Sync that folder with Google Drive for Desktop, then send "
                   f"`!source {cid} \"G:\\My Drive\\<folder name>\"`:\n" + "\n".join(drive[:3]))
    if problems:
        out.append("Couldn't use: " + "; ".join(problems[:3]))
    if has_content:
        campaigns.activate(cid)
        out.append("**The lane is running.** Clips will show up here for review.")
    else:
        out.append(f"No footage link found yet. Send `!source {cid} <YouTube/Twitch channel, video or folder>` "
                   f"and then `!activate {cid}`.")
    return "\n".join(out)


# ---------- #campaign-feed ----------

def looks_like_campaign(text):
    return len(text or "") >= 40 and bool(FEED_HINTS.search(text))


def from_feed(text, message_id):
    """One message in #campaign-feed -> a suggestion with Join buttons. Returns the campaign id or None."""
    key = f"discord:{message_id}"
    if db.one("SELECT 1 FROM discovered WHERE key=?", key):
        return None
    db.x("INSERT OR REPLACE INTO discovered(key, campaign_id, created_at) VALUES(?,?,?)", key, None, iso(utcnow()))
    if not looks_like_campaign(text):
        return None
    from . import discovery
    data, provider = campaigns.extract_from_brief(text)
    if not data.get("url"):
        links = [u.rstrip(').,') for u in re.findall(r"https?://\S+", text) if "discord" not in u]
        data["url"] = next((u for u in links if "whop.com" in u), links[0] if links else "")
    cid, _ = discovery.add_from_data(data, text, provider, key, origin="feed", notify_always=True)
    return cid
