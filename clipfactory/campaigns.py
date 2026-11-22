"""Campaign lanes: each clipping campaign has its own sources, rules, budget, quota and earnings."""
import datetime as dt
import json
import re

import yaml

from . import config, db
from .util import ET, iso, parse_iso, utcnow

JSON_FIELDS = ("platforms", "accounts", "required_hashtags", "rules")
POSTABLE = ("youtube", "instagram", "facebook", "tiktok")
LAYOUTS = ("framed", "blur", "facecam_split", "crop")
FIELDS = ("name", "platform", "creator", "url", "topic", "audience", "rate_per_1k", "budget_left",
          "max_payout_per_clip", "min_views", "ends", "daily_clip_quota", "platforms", "accounts",
          "required_hashtags", "credit", "min_len", "max_len", "min_score", "max_moments_per_video", "priority",
          "rules", "brief")


def _row(r):
    if not r:
        return r
    for f in JSON_FIELDS:
        r[f] = json.loads(r[f] or ("{}" if f == "accounts" else "[]"))
    return r


def get(campaign_id):
    return _row(db.one("SELECT * FROM campaigns WHERE id=?", campaign_id))


def all_campaigns():
    return [_row(r) for r in db.q("SELECT * FROM campaigns ORDER BY paused, priority, rate_per_1k DESC")]


def lane_state(c):
    if c.get("stage") in ("suggested", "ignored"):
        return c["stage"]
    if c["paused"]:
        return "paused"
    if c["ends"] and c["ends"] < dt.datetime.now(ET).date().isoformat():
        return "ended"
    if c["budget_left"] is not None and c["budget_left"] <= 0:
        return "budget used"
    return "running"


def active():
    return [c for c in all_campaigns() if lane_state(c) == "running"]


def sources(campaign_id):
    rows = db.q("SELECT * FROM sources WHERE campaign_id=? ORDER BY id", campaign_id)
    for r in rows:
        r["facecam"] = json.loads(r["facecam"]) if r["facecam"] else None
    return rows


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40].strip("-")
    return s or "campaign"


def _norm(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def find_same(name):
    """A campaign already known by this name (any stage, ignored ones too), or None."""
    target = _norm(name)
    if not target:
        return None
    for c in all_campaigns():
        if _norm(c["name"]) == target:
            return c
    return None


def remove_duplicate_suggestions():
    """Keep the first suggestion of each name; delete the repeats. Returns how many were removed."""
    seen, removed = set(), 0
    for c in sorted(all_campaigns(), key=lambda c: (c.get("found_at") or "", c["id"])):
        key = _norm(c["name"])
        if c.get("stage") == "suggested" and key in seen:
            db.x("DELETE FROM campaigns WHERE id=?", c["id"])
            removed += 1
        seen.add(key)
    return removed


# ---------- sources ----------

def parse_source(line):
    """Turn a pasted line into (kind, id). Accepts YouTube channel links or @handles, Twitch channels,
    and folders on this computer. Single video links return kind 'video'."""
    line = line.strip()
    if not line:
        return None
    if re.match(r"^(/|~|[A-Za-z]:[\\/])", line):
        return "folder", line
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", line) or re.fullmatch(r"(UC[\w-]{22})", line)
    if m:
        return "youtube_channel", m.group(1)
    m = re.search(r"youtube\.com/(@[\w.-]+)", line) or re.fullmatch(r"(@[\w.-]+)", line)
    if m:
        return "youtube_channel", m.group(1)
    if re.search(r"(youtube\.com/(watch|live|shorts)|youtu\.be/|twitch\.tv/videos/|kick\.com/.+/videos?/)", line):
        return "video", line
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)/?$", line)
    if m:
        return "twitch_channel", m.group(1).lower()
    raise ValueError(f"Can't use this source: {line}. Use a YouTube channel, a Twitch channel, a video link "
                     "or a folder path.")


def set_sources(campaign_id, items, default_layout="blur"):
    """items: list of dicts with kind, channel_id, name?, streams_only?, layout?, facecam?"""
    keep = []
    for s in items:
        fc = s.get("facecam")
        db.x("""INSERT INTO sources(campaign_id, kind, name, channel_id, streams_only, layout, facecam)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(campaign_id, channel_id) DO UPDATE SET kind=excluded.kind, name=excluded.name,
                  streams_only=excluded.streams_only, layout=excluded.layout,
                  facecam=COALESCE(excluded.facecam, sources.facecam)""",
             campaign_id, s["kind"], s.get("name") or s["channel_id"], s["channel_id"],
             int(bool(s.get("streams_only"))), s.get("layout") or default_layout, json.dumps(fc) if fc else None)
        keep.append(s["channel_id"])
    if keep:
        db.x(f"DELETE FROM sources WHERE campaign_id=? AND channel_id NOT IN ({','.join('?' * len(keep))})",
             campaign_id, *keep)
    else:
        db.x("DELETE FROM sources WHERE campaign_id=?", campaign_id)


def add_source(campaign_id, line):
    """Add one channel/folder (watched) or video (clipped once) to a campaign. Returns a reply."""
    kind, ident = parse_source(line)
    if kind == "video":
        from . import watcher
        vid = watcher.add_video(ident, campaign_id)
        return f"Added video `{vid}`. It will be clipped once the lane is active."
    layout = "facecam_split" if kind == "twitch_channel" else "blur"
    db.x("""INSERT INTO sources(campaign_id, kind, name, channel_id, streams_only, layout) VALUES(?,?,?,?,?,?)
            ON CONFLICT(campaign_id, channel_id) DO NOTHING""",
         campaign_id, kind, ident, ident, int(kind == "twitch_channel"), layout)
    what = {"youtube_channel": "YouTube channel", "twitch_channel": "Twitch channel", "folder": "folder"}[kind]
    return f"Added {what} `{ident}`. New streams/files there will be clipped."


# ---------- saving ----------

def _clean(c):
    def num(v, cast=float, default=None):
        if v in (None, ""):
            return default
        return cast(str(v).replace("$", "").replace(",", "").strip())
    lo, hi = (c.get("clip_length") or [c.get("min_len") or 12, c.get("max_len") or 30])[:2]
    tags = c.get("required_hashtags") or []
    if isinstance(tags, str):
        tags = tags.split()
    tags = ["#" + t.lstrip("#") for t in tags if t.strip("#")]
    platforms = [p for p in (c.get("platforms") or ["youtube"]) if p in POSTABLE] or ["youtube"]
    rules = c.get("rules") or []
    if isinstance(rules, str):
        rules = [r.strip(" -•") for r in rules.splitlines() if r.strip(" -•")]
    accounts = c.get("accounts") or {}
    return {
        "name": (c.get("name") or "Untitled campaign").strip(),
        "platform": (c.get("platform") or "").strip(),
        "creator": (c.get("creator") or "").strip(),
        "url": (c.get("url") or "").strip(),
        "topic": (c.get("topic") or "gaming").strip(),
        "audience": (c.get("audience") or "US viewers aged 16 to 34").strip(),
        "rate_per_1k": num(c.get("rate_per_1k"), default=0.0),
        "budget_left": num(c.get("budget_left")),
        "max_payout_per_clip": num(c.get("max_payout_per_clip")),
        "min_views": num(c.get("min_views"), int, 0),
        "ends": str(c["ends"]) if c.get("ends") else None,
        "daily_clip_quota": num(c.get("daily_clip_quota"), int, 3),
        "platforms": json.dumps(platforms),
        "accounts": json.dumps({p: accounts.get(p, "main") for p in platforms}),
        "required_hashtags": json.dumps(tags),
        "credit": (c.get("credit") or "").strip(),
        "min_len": int(num(lo, float, 12)),
        "max_len": int(num(hi, float, 30)),
        "min_score": num(c.get("min_score"), default=7.0),
        "max_moments_per_video": num(c.get("max_moments_per_video"), int, 8),
        "priority": num(c.get("priority"), int, 5),
        "rules": json.dumps(rules[:12]),
        "brief": (c.get("brief") or "")[:20000],
        "auto_approve_score": num(c.get("auto_approve_score")),
    }


def save(cid, data, origin):
    row = _clean(data)
    cols = list(row)
    db.x(f"""INSERT INTO campaigns(id, {', '.join(cols)}, origin) VALUES(?, {', '.join('?' * len(cols))}, ?)
             ON CONFLICT(id) DO UPDATE SET {', '.join(f'{k}=excluded.{k}' for k in cols)}""",
         cid, *row.values(), origin)
    return cid


def sync(path=None):
    """Load campaigns.yaml. Campaigns removed from the file are paused, not deleted."""
    path = path or config.path("campaigns.yaml")
    with open(path, encoding="utf-8") as f:
        items = (yaml.safe_load(f) or {}).get("campaigns") or []
    seen = set()
    for c in items:
        cid = c["id"]
        seen.add(cid)
        existing = db.one("SELECT origin FROM campaigns WHERE id=?", cid)
        if existing and existing["origin"] == "dashboard":
            continue  # edited on the dashboard; the dashboard version wins
        save(cid, c, "yaml")
        db.x("UPDATE campaigns SET paused=0, notes='' WHERE id=? AND notes='Removed from campaigns.yaml'", cid)
        srcs = []
        for s in c.get("sources") or []:
            kind = s.get("kind", "youtube_channel")
            ident = s.get("channel_id") or s.get("channel") or s.get("path")
            if not ident:
                continue
            srcs.append({**s, "kind": kind, "channel_id": ident})
        set_sources(cid, srcs)
    for r in db.q("SELECT id FROM campaigns WHERE demo=0 AND origin='yaml'"):
        if r["id"] not in seen:
            db.x("UPDATE campaigns SET paused=1, notes='Removed from campaigns.yaml' WHERE id=?", r["id"])
    return len(items)


def set_paused(campaign_id, paused):
    db.x("UPDATE campaigns SET paused=? WHERE id=?", int(paused), campaign_id)
    db.log(campaign_id, "Lane paused" if paused else "Lane resumed")


def auto_approve_threshold(c):
    if c.get("auto_approve_score") is not None:
        return c["auto_approve_score"]
    return config.section("review").get("auto_approve_score", 9)


def new_id(name):
    base = slug(name)
    cid, n = base, 2
    while get(cid):
        cid, n = f"{base}-{n}", n + 1
    return cid


def suggest(data, origin="discovered"):
    """Save a campaign the AI read as a suggestion. It does nothing until you activate it."""
    cid = new_id(data.get("name"))
    save(cid, data, origin)
    srcs, videos = [], []
    for line in data.get("sources") or []:
        try:
            parsed = parse_source(str(line))
        except ValueError:
            continue
        if parsed and parsed[0] == "video":
            videos.append(parsed[1])
        elif parsed:
            srcs.append({"kind": parsed[0], "channel_id": parsed[1], "streams_only": parsed[0] == "twitch_channel"})
    set_sources(cid, srcs)
    db.x("UPDATE campaigns SET stage='suggested', found_at=?, notes=? WHERE id=?", iso(utcnow()),
         json.dumps(_notes(data, videos)), cid)
    return cid


def _notes(data, videos):
    return {"videos": videos, "unsupported_platforms": data.get("unsupported_platforms") or [],
            "no_supported_platform": not data.get("platforms"),
            "clippers": data.get("clippers"), "requires_application": data.get("requires_application")}


def refresh_suggestion(cid, data):
    """Re-save a suggestion from better data (e.g. the scraper's own fields), keeping its listing text."""
    c = get(cid)
    data = {**data, "brief": c.get("brief") or data.get("brief") or ""}
    save(cid, data, c.get("origin") or "discovered")
    db.x("UPDATE campaigns SET notes=? WHERE id=?", json.dumps(_notes(data, _extra(c).get("videos") or [])), cid)


def _extra(c):
    try:
        return json.loads(c["notes"] or "{}")
    except json.JSONDecodeError:
        return {}


def activate(cid):
    c = get(cid)
    if not c:
        return None
    db.x("UPDATE campaigns SET stage='active', paused=0 WHERE id=?", cid)
    from . import watcher
    for v in _extra(c).get("videos") or []:
        try:
            watcher.add_video(v, cid)
        except (Exception, SystemExit) as e:
            db.log(cid, f"Could not add {v}: {e}", "error")
    db.log(cid, "Campaign activated")
    return get(cid)


def ignore(cid):
    db.x("UPDATE campaigns SET stage='ignored', paused=1 WHERE id=?", cid)
    db.log(cid, "Campaign ignored")


def fit(c):
    """Why a found campaign is or isn't worth joining: (ok, reasons)."""
    d = config.section("discovery")
    reasons, ok = [], True
    extra = _extra(c)
    if extra.get("no_supported_platform"):
        ok = False
        reasons.append("pays only on platforms the tool can't post to (" +
                       ", ".join(extra.get("unsupported_platforms") or ["unknown"]) + ")")
    if not extra.get("no_supported_platform"):
        from . import publish
        ready = [p for p in c["platforms"] if publish.platform_ready(p, c["accounts"].get(p, "main"))]
        missing = [p for p in c["platforms"] if p not in ready]
        by_hand = [p for p in ready if publish.mode(p, c["accounts"].get(p, "main")) == "manual"]
        if not ready:
            ok = False
            reasons.append(f"pays for {', '.join(c['platforms'])}, which you haven't connected yet")
        elif missing:
            reasons.append(f"{', '.join(missing)} not connected yet, so it posts to {', '.join(ready)} only")
        if by_hand:
            reasons.append(f"you post the {', '.join(by_hand)} clips yourself (the tool prepares them)")
        if extra.get("unsupported_platforms"):
            reasons.append(f"also pays for {', '.join(extra['unsupported_platforms'])} (the tool can't post there)")
    if extra.get("requires_application"):
        reasons.append("you have to apply and be accepted first")
    if not c["rate_per_1k"]:
        reasons.append("pay rate isn't in the listing; check it on the campaign page")
    elif c["rate_per_1k"] < d.get("min_rate_per_1k", 0.5):
        ok = False
        reasons.append(f"rate ${c['rate_per_1k']:.2f} per 1K is under your minimum")
    if c["budget_left"] is not None and c["budget_left"] < d.get("min_budget_left", 500):
        ok = False
        reasons.append(f"only ${c['budget_left']:,.0f} budget left")
    if c["ends"] and c["ends"] < dt.datetime.now(ET).date().isoformat():
        ok = False
        reasons.append("already ended")
    if not db.one("SELECT 1 FROM sources WHERE campaign_id=?", c["id"]) and not extra.get("videos"):
        reasons.append("no content links yet (they usually appear after you join): add them with "
                       f"`!source {c['id']} <link>`")
    terms = " ".join([c.get("brief") or ""] + [str(r) for r in c.get("rules") or []])
    if re.search(r"\b(india|indian)\b", terms, re.I) or \
            re.search(r"\b(tier[ -]?1|us[ -]only|usa[ -]only|united states only|us viewers only)\b", terms, re.I):
        reasons.append("⚠ views may only pay from certain countries (check the rules): "
                       "post for a US audience, Indian views may not count")
    interests = [i.lower() for i in d.get("interests") or []]
    hay = f"{c['name']} {c['topic']} {c['creator']}".lower()
    if interests and not any(i in hay for i in interests):
        ok = False
        reasons.append("outside your interests")
    return ok, reasons


# ---------- AI reads a pasted campaign page ----------

EXTRACT_PROMPT = """Below is the text of a clipping campaign page (for example from Whop). A creator pays clippers
to post short clips of their content. Extract the campaign's terms.

Return JSON only, with these keys (use null when the page doesn't say):
{{"name": "short campaign name", "platform": "site hosting the campaign, e.g. Whop", "creator": "creator or brand",
  "url": "link to the campaign page itself, if shown",
  "topic": "what the content is about, e.g. 'IRL streams', 'GTA 6 gameplay', 'business podcast'",
  "rate_per_1k": number in USD per 1,000 views, "budget_left": number in USD, "max_payout_per_clip": number in USD,
  "min_views": number of views before a clip pays, "ends": "YYYY-MM-DD",
  "platforms": ["youtube", "instagram", "facebook", "tiktok", "x"] (only those the campaign accepts),
  "required_hashtags": ["#tag"], "credit": "credit line or tag the clip must include",
  "clip_length": [min_seconds, max_seconds],
  "sources": ["links to the creator's channels, VODs or content folders"],
  "rules": ["each other rule, max 12 words each, max 10 rules"]}}

Campaign page:
{text}"""


def _extract_validate(d):
    from . import llm
    if not isinstance(d, dict) or not d.get("name"):
        raise llm.Invalid("no campaign name")
    plats = [str(p).lower().strip() for p in d.get("platforms") or []]
    plats = ["youtube" if p in ("youtube shorts", "shorts", "yt") else
             "instagram" if p in ("instagram reels", "reels", "ig") else p for p in plats]
    d["unsupported_platforms"] = [p for p in plats if p not in POSTABLE]
    d["platforms"] = [p for p in plats if p in POSTABLE]
    if not isinstance(d.get("sources") or [], list) or not isinstance(d.get("rules") or [], list):
        raise llm.Invalid("sources and rules must be lists")
    llm.require_english(d.get("topic") or "", " ".join(map(str, d.get("rules") or [])))
    return d


def extract_from_brief(text):
    from . import llm
    data, provider = llm.complete_json("campaign_extract", EXTRACT_PROMPT.format(text=text[:15000]),
                                       _extract_validate)
    data["brief"] = text
    return data, provider


# ---------- stats ----------

def post_earning(c, views):
    if views < (c["min_views"] or 0) or not c["rate_per_1k"]:
        return 0.0
    usd = views / 1000 * c["rate_per_1k"]
    if c["max_payout_per_clip"]:
        usd = min(usd, c["max_payout_per_clip"])
    return round(usd, 2)


def lane_stats(c):
    cid = c["id"]
    count = lambda sql, *a: db.one(sql, cid, *a)["n"]  # noqa: E731
    posts = db.q("SELECT views FROM posts WHERE campaign_id=? AND status IN ('posted','uploaded')", cid)
    today = dt.datetime.now(ET).date().isoformat()
    return {
        "sources": count("SELECT COUNT(*) n FROM sources WHERE campaign_id=?"),
        "videos_waiting": count("SELECT COUNT(*) n FROM videos WHERE campaign_id=? AND status IN ('new','analyzing','waiting')"),
        "videos": count("SELECT COUNT(*) n FROM videos WHERE campaign_id=?"),
        "moments": count("SELECT COUNT(*) n FROM moments WHERE campaign_id=? AND status IN ('found','downloading')"),
        "moments_total": count("SELECT COUNT(*) n FROM moments WHERE campaign_id=?"),
        "editing": count("SELECT COUNT(*) n FROM clips WHERE campaign_id=? AND status IN ('downloaded','rendered')"),
        "review": count("SELECT COUNT(*) n FROM clips WHERE campaign_id=? AND status='pending_review'"),
        "scheduled": count("SELECT COUNT(*) n FROM posts WHERE campaign_id=? AND status IN ('queued','uploaded')"),
        "posted": count("SELECT COUNT(*) n FROM posts WHERE campaign_id=? AND status='posted'"),
        "posted_today": len({r["clip_id"] for r in db.q(
            "SELECT clip_id, scheduled_at FROM posts WHERE campaign_id=?", cid)
            if r["scheduled_at"] and parse_iso(r["scheduled_at"]).astimezone(ET).date().isoformat() == today}),
        "to_submit": count("""SELECT COUNT(*) n FROM posts p JOIN campaigns c ON c.id=p.campaign_id
                              WHERE p.campaign_id=? AND p.status='posted' AND p.submitted=0
                              AND c.url IS NOT NULL AND c.url != ''"""),
        "views": sum(p["views"] or 0 for p in posts),
        "earned": round(sum(post_earning(c, p["views"] or 0) for p in posts), 2),
        "state": lane_state(c),
    }


def today_et():
    return utcnow().astimezone(ET).date()
