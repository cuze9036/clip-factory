"""The #campaigns board: one Discord message per live campaign, kept up to date by the bot.

- New campaigns appear, changed numbers are edited in place, ended / ignored / duplicate ones are deleted.
- Duplicates (same campaign name) keep only the freshest one.
- Every card has a priority tag: 🥇 FIRST, 🥈 SECOND or ⚪ LOW, from the pay numbers plus an AI judgement of how
  quick and easy the clips are to make. Running lanes sit at the bottom with ▶ RUNNING.

This module only decides what the board should show (plan) and what to change (ops). discord_bot.py does the
Discord calls.
"""
import datetime as dt
import hashlib
import json

from . import campaigns, config, db, llm
from .util import ET, iso, parse_iso, utcnow

TIERS = {"low": "⚪ LOW PRIORITY", "second": "🥈 SECOND PRIORITY", "first": "🥇 FIRST PRIORITY",
         "running": "▶ RUNNING"}
ORDER = ["low", "second", "first", "running"]   # top to bottom: the best ones end up next to the message box
EFFORT_ICON = {"easy": "🟢 easy", "medium": "🟡 medium", "hard": "🔴 hard"}

EFFORT_PROMPT = """A clipping campaign pays clippers per 1,000 views of short vertical clips they post.
Judge how quick and easy it is for ONE person with an automated clipping tool (it finds the best moments in long
videos and edits vertical clips with captions) to make clips that follow this campaign's rules.

easy: long streams, podcasts or videos with many moments, footage easy to get (YouTube, Twitch, a shared folder),
      few creative rules.
medium: some rules to follow (specific moments, tags, face on screen), or footage only after joining.
hard: needs original filming, product shots, scripted brand messages, strict creative briefs, or very little footage.

Return JSON only: {{"effort": "easy" | "medium" | "hard", "why": "max 12 words, plain English"}}

Campaign: {name}
Creator or brand: {creator}
Topic: {topic}
Platforms: {platforms}
Rules: {rules}
Page text (start): {brief}
"""


# ---------- AI: how easy is it? ----------

def _effort_validate(d):
    if not isinstance(d, dict) or d.get("effort") not in EFFORT_ICON:
        raise llm.Invalid("effort must be easy, medium or hard")
    return {"effort": d["effort"], "why": str(d.get("why") or "").strip()[:100]}


def effort(c, allow_ai=True):
    """{'effort', 'why'} saved on the campaign after the first AI look; None until then."""
    extra = campaigns._extra(c)
    if extra.get("effort") in EFFORT_ICON:
        return {"effort": extra["effort"], "why": extra.get("effort_why", "")}
    if not allow_ai:
        return None
    brief = c.get("brief") or ""
    try:
        data, _ = llm.complete_json("campaign_effort", EFFORT_PROMPT.format(
            name=c["name"], creator=c["creator"] or "?", topic=c["topic"] or "?",
            platforms=", ".join(c["platforms"]) or "?", rules="; ".join(c["rules"][:10]) or "none listed",
            brief=brief[:1500] or "(not available)"), _effort_validate)
    except llm.LLMError:
        return None
    extra.update(effort=data["effort"], effort_why=data["why"])
    db.x("UPDATE campaigns SET notes=? WHERE id=?", json.dumps(extra), c["id"])
    return data


# ---------- priority ----------

def score(c, eff):
    """Higher is better. Pay rate, budget each clipper can still earn, effort, and whether you can post it."""
    extra = campaigns._extra(c)
    clippers = extra.get("clippers") or 0
    budget = c["budget_left"] or 0
    s = min(c["rate_per_1k"] or 0, 3) * 2                       # 0..6
    per = budget / max(clippers, 1) if budget else 0
    s += 3 if per >= 1000 else 2 if per >= 300 else 1 if per >= 100 else 0
    s += {"easy": 2, "medium": 1, "hard": 0}.get((eff or {}).get("effort"), 1)
    if extra.get("requires_application"):
        s -= 1
    return s


def tier(c, eff):
    ok, _ = campaigns.fit(c)
    if not ok or (eff or {}).get("effort") == "hard":
        return "low"
    s = score(c, eff)
    return "first" if s >= 8 else "second" if s >= 5 else "low"


# ---------- what the board should show ----------

def _fresh(c):
    return c.get("seen_at") or c.get("found_at") or ""


def _dedupe(cs):
    """One campaign per name: the freshest."""
    best = {}
    for c in cs:
        k = campaigns._norm(c["name"])
        if k not in best or _fresh(c) > _fresh(best[k]):
            best[k] = c
    return list(best.values())


def card(c, t, eff):
    from .commands import _money, _short_name, _where
    extra = campaigns._extra(c)
    clippers = extra.get("clippers") or 0
    nums = [f"${c['rate_per_1k']:.2f} per 1K views" if c["rate_per_1k"] else "pay: see the campaign page"]
    if c["budget_left"] is not None:
        nums.append(f"{_money(c['budget_left'])} left")
    if clippers:
        nums.append(f"{clippers:,} clippers")
        if c["budget_left"]:
            nums.append(f"~{_money(c['budget_left'] / clippers)} each")
    if c["max_payout_per_clip"]:
        nums.append(f"max {_money(c['max_payout_per_clip'])}/clip")
    lines = [f"**{TIERS[t]}**", f"**{_short_name(c['name'])}**", " · ".join(nums)]
    if eff:
        lines.append(f"Effort: {EFFORT_ICON[eff['effort']]}" + (f" · {eff['why']}" if eff.get("why") else ""))
    lines.append(f"Posts to: {_where(c)}")
    if t == "running":
        s = campaigns.lane_stats(c)
        lines.append(f"Clips: review {s['review']} · scheduled {s['scheduled']} · posted {s['posted']} · "
                     f"{s['views']:,} views · ~${s['earned']:,.2f}")
    else:
        _, reasons = campaigns.fit(c)
        if reasons:
            lines.append("Heads up: " + "; ".join(reasons[:2]))
        if c.get("origin") == "feed":
            lines.append("From #campaign-feed")
    return "\n".join(lines)[:1900]


def plan(max_ai=5):
    """Ordered list of {key, text, buttons, tier} for the board, top to bottom, ending with the summary."""
    from . import joinflow
    asked = 0
    items = []
    running = [c for c in campaigns.all_campaigns() if campaigns.lane_state(c) == "running"
               and c.get("origin") != "yaml"]
    for c in running:
        items.append(({"key": f"c:{c['id']}", "tier": "running", "sort": -(c["rate_per_1k"] or 0)},
                      c, effort(c, allow_ai=False)))
    for c in _dedupe(joinflow.live_campaigns()):
        need_ai = not campaigns._extra(c).get("effort")
        eff = effort(c, allow_ai=need_ai and asked < max_ai)
        if need_ai and asked < max_ai:
            asked += 1   # a few AI looks per refresh; the rest get theirs on the next refresh
        t = tier(c, eff)
        items.append(({"key": f"c:{c['id']}", "tier": t, "sort": -score(c, eff)}, c, eff))
    items.sort(key=lambda x: (ORDER.index(x[0]["tier"]), -x[0]["sort"]))   # worst first, best last
    out = []
    for meta, c, eff in items:
        buttons = [] if meta["tier"] == "running" else joinflow.buttons(c)
        out.append({"key": meta["key"], "tier": meta["tier"], "text": card(c, meta["tier"], eff), "buttons": buttons})
    counts = {t: sum(1 for i in out if i["tier"] == t) for t in ORDER}
    now = dt.datetime.now(ET)
    summary = (f"**Campaign board** · updated {now:%b %d, %I:%M %p} ET\n"
               f"🥇 {counts['first']} first · 🥈 {counts['second']} second · ⚪ {counts['low']} low · "
               f"▶ {counts['running']} running\n"
               "Priority = pay per 1K + budget left per clipper + how easy the clips are (AI) + whether you can "
               "post there. Tap **Open campaign**, join on Whop, then **I joined** and paste the page.")
    out.append({"key": "summary", "tier": "summary", "text": summary, "buttons": []})
    return out


# ---------- what to change on Discord ----------

def digest(item):
    text = item["text"] if item["key"] != "summary" else item["text"].split("\n", 1)[1]  # ignore the clock
    return hashlib.sha1(json.dumps([text, item["buttons"]]).encode()).hexdigest()


def current():
    return db.q("SELECT * FROM board ORDER BY pos")


def ops(desired, now_rows):
    """'repost' when the set or tier groups changed (Discord can't reorder messages), else the keys to edit."""
    have = [r["key"] for r in now_rows]
    want = [d["key"] for d in desired]
    groups = lambda rows: [(r["tier"], r["key"]) for r in rows]  # noqa: E731
    have_groups = sorted(groups(now_rows))
    want_groups = sorted((d["tier"], d["key"]) for d in desired)
    if set(have) != set(want) or have_groups != want_groups or len(have) != len(want):
        return "repost", want
    by_key = {r["key"]: r for r in now_rows}
    return "edit", [d["key"] for d in desired if by_key[d["key"]]["hash"] != digest(d)]


def due(minutes=15):
    """The summary's clock is refreshed at least this often even when nothing else changed."""
    last = db.kv_get("board_last_full")
    return not last or utcnow() - parse_iso(last) >= dt.timedelta(minutes=minutes)


def mark_full():
    db.kv_set("board_last_full", iso(utcnow()))


def channel_name():
    return (config.section("discord").get("board_channel") or "campaigns").lstrip("#")
