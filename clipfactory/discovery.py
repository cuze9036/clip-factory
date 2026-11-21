"""Find new clipping campaigns, let the AI read each one, and suggest the good ones on Discord.

Whop has no official API for clippers, so listings come from a scraper you choose (default: an Apify
actor that reads Whop's public Content Rewards listings, run with your own APIFY_TOKEN). Nothing here
logs in to Whop or joins campaigns: you join on Whop yourself, then tap Activate."""
import hashlib
import json
import re

import requests

from . import campaigns, config, db, notify
from .util import iso, utcnow


def fetch_listings():
    d = config.section("discovery")
    token = config.env("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN is not set in .env (or turn discovery off in config.yaml)")
    actor = d.get("apify_actor", "fayoussef~whop-clipping-campaigns-scraper")
    r = requests.post(f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
                      params={"token": token, "clean": "true", "limit": d.get("max_listings", 40)},
                      json=d.get("apify_input") or {}, timeout=300)
    r.raise_for_status()
    items = r.json()
    return items if isinstance(items, list) else []


IMAGE_HINTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", "img", "image", "cdn", "avatar", "logo")


def _name(item):
    if not isinstance(item, dict):
        return ""
    for k in ("title", "name", "campaignName", "campaign_name", "campaignTitle"):
        if isinstance(item.get(k), str) and item[k].strip():
            return item[k].strip()
    return ""


def _key(item):
    """A stable id for a listing: its campaign link if there is one, else its name."""
    if isinstance(item, dict):
        for k, v in item.items():
            if isinstance(v, str) and "whop.com" in v and any(w in k.lower() for w in ("url", "link", "href")) \
                    and not any(h in v.lower() for h in IMAGE_HINTS):
                return v.split("?")[0].rstrip("/")
        if _name(item):
            return "name:" + campaigns._norm(_name(item))
    return hashlib.sha1(json.dumps(item, sort_keys=True).encode()).hexdigest()


def summary(c):
    ok, reasons = campaigns.fit(c)
    srcs = campaigns.sources(c["id"])
    lines = [
        f"**{c['name']}**" + (f" by {c['creator']}" if c["creator"] else "") + (f" · {c['platform']}" if c["platform"] else ""),
        f"{c['topic']} · ${c['rate_per_1k']:.2f} per 1K views"
        + (f" · ${c['budget_left']:,.0f} budget left" if c["budget_left"] is not None else "")
        + (f" · pays after {c['min_views']:,} views" if c["min_views"] else "")
        + (f" · {campaigns._extra(c)['clippers']} clippers" if campaigns._extra(c).get("clippers") else ""),
        "Posts to: " + ", ".join(c["platforms"]) + " · Sources: "
        + (", ".join(s["name"] for s in srcs) or "none found"),
    ]
    if reasons:
        lines.append("Heads up: " + "; ".join(reasons))
    if c["url"]:
        lines.append(f"1) Join it on {c['platform'] or 'the campaign page'}: {c['url']}")
        lines.append("2) Then tap Activate and the lane starts.")
    return "\n".join(lines), ok


PLATFORM_MAP = {"youtube": "youtube", "youtube_shorts": "youtube", "shorts": "youtube", "instagram": "instagram",
                "instagram_reels": "instagram", "reels": "instagram", "facebook": "facebook", "facebook_reels": "facebook", "tiktok": "tiktok"}


def from_listing(item):
    """The scraper's own fields -> campaign data, with no AI call. None when the listing isn't structured."""
    if not isinstance(item, dict) or not ({"rewardPerThousandUsd", "socialPlatforms", "budgetLeftUsd"} & set(item)):
        return None
    title = (item.get("title") or item.get("name") or "").strip()
    if not title:
        return None
    raw = [str(p).lower().strip().replace(" ", "_") for p in item.get("socialPlatforms") or []]
    platforms = list(dict.fromkeys(PLATFORM_MAP[p] for p in raw if p in PLATFORM_MAP))
    url = item.get("campaignUrl") or ""
    rate = item.get("rewardPerThousandUsd") if str(item.get("payoutType", "cpm")).lower() == "cpm" else None
    left = item.get("budgetLeftUsd")
    return {
        "name": title, "platform": "Whop", "creator": item.get("brand") or "",
        "url": url if url.startswith("http") else "",
        "topic": re.sub(r"\s*clipping\s*(campaign)?\s*$", "", title, flags=re.I) or title,
        "rate_per_1k": rate or 0, "budget_left": round(left) if isinstance(left, (int, float)) else None,
        "platforms": platforms, "unsupported_platforms": [p for p in raw if p not in PLATFORM_MAP],
        "sources": [], "rules": [], "clippers": item.get("creatorsCount"),
        "requires_application": bool(item.get("requiresApplication")),
    }


def refresh_from_listings():
    """Suggestions read before the scraper's fields were used directly: re-read them from the saved listing."""
    for c in campaigns.all_campaigns():
        if c.get("stage") != "suggested" or c.get("origin") != "discovered":
            continue
        try:
            data = from_listing(json.loads(c.get("brief") or ""))
        except (json.JSONDecodeError, TypeError):
            continue
        if data:
            campaigns.refresh_suggestion(c["id"], data)


def link_from_text(text):
    """The campaign's whop.com page in a listing, or "". Other links (ads, images) never count."""
    links = [u.rstrip('",).') for u in re.findall(r"https?://(?:www\.)?whop\.com/[^\s\"'<>]+", text or "")
             if not any(h in u.lower() for h in IMAGE_HINTS)]
    return links[0].split("?")[0] if links else ""


def backfill_links():
    """Found campaigns without a proper whop.com link: pull it from the saved listing, or clear a wrong one."""
    for c in campaigns.all_campaigns():
        if c.get("stage") != "suggested" or c.get("origin") != "discovered":
            continue
        if c["url"] and "whop.com" in c["url"]:
            continue
        db.x("UPDATE campaigns SET url=? WHERE id=?", link_from_text(c.get("brief")), c["id"])


def add_from_text(text, key=None, origin="discovered", notify_always=True):
    """AI reads one campaign (a scraped listing or text you pasted) and saves it as a suggestion."""
    data, provider = campaigns.extract_from_brief(text)
    return add_from_data(data, text, provider, key, origin, notify_always)


def add_from_data(data, text, provider, key=None, origin="discovered", notify_always=True):
    data.setdefault("brief", text)
    if key and key.startswith("http") and not data.get("url"):
        data["url"] = key
    if origin == "discovered" and "whop.com" not in (data.get("url") or ""):
        data["url"] = link_from_text(text)
    same = campaigns.find_same(data.get("name"))
    if same:
        if same.get("stage") == "suggested":  # refresh the numbers on the one we already have
            for f in ("rate_per_1k", "budget_left", "min_views"):
                if data.get(f) is not None:
                    db.x(f"UPDATE campaigns SET {f}=? WHERE id=?", data[f], same["id"])
        if key:
            db.x("INSERT OR REPLACE INTO discovered(key, campaign_id, created_at) VALUES(?,?,?)", key, same["id"],
                 iso(utcnow()))
        if origin != "discovered":
            msg, _ = summary(campaigns.get(same["id"]))
            notify.send("campaign", "You already have this campaign.\n" + msg, campaign_id=same["id"],
                        buttons=[["Activate", f"act:{same['id']}", "green"], ["Ignore", f"ign:{same['id']}", "grey"]]
                        if same.get("stage") == "suggested" else [])
        return same["id"], False
    cid = campaigns.suggest(data, origin)
    if key:
        db.x("INSERT OR REPLACE INTO discovered(key, campaign_id, created_at) VALUES(?,?,?)", key, cid, iso(utcnow()))
    c = campaigns.get(cid)
    msg, ok = summary(c)
    if ok or notify_always:
        notify.send("campaign", ("New campaign found\n" if origin == "discovered" else "") + msg, campaign_id=cid,
                    buttons=[["Activate", f"act:{cid}", "green"], ["Ignore", f"ign:{cid}", "grey"]])
    db.log(cid, f"Campaign suggested ({'good fit' if ok else 'weak fit'}, read by {provider})")
    return cid, ok


def run():
    d = config.section("discovery")
    if not d.get("enabled"):
        return 0
    found = 0
    campaigns.remove_duplicate_suggestions()
    refresh_from_listings()
    backfill_links()
    try:
        items = fetch_listings()
    except Exception as e:
        db.log(None, f"Campaign finder failed: {e}", "error")
        db.kv_set("discovery_last_run", iso(utcnow()))
        return 0
    for item in items:
        key = _key(item)
        if db.one("SELECT 1 FROM discovered WHERE key=?", key):
            continue
        if _name(item) and campaigns.find_same(_name(item)):
            db.x("INSERT OR REPLACE INTO discovered(key, campaign_id, created_at) VALUES(?,?,?)", key,
                 campaigns.find_same(_name(item))["id"], iso(utcnow()))
            continue
        try:
            text = json.dumps(item, ensure_ascii=False)[:12000]
            data = from_listing(item)
            if data:
                _, ok = add_from_data(data, text, "listing fields", key, notify_always=False)
            else:
                _, ok = add_from_text(text, key, notify_always=False)
            found += int(ok)
        except Exception as e:
            db.x("INSERT OR REPLACE INTO discovered(key, campaign_id, created_at) VALUES(?,?,?)", key, None, iso(utcnow()))
            db.log(None, f"Couldn't read a campaign listing: {e}", "warn")
        if found >= d.get("max_suggestions_per_run", 5):
            break
    db.kv_set("discovery_last_run", iso(utcnow()))
    return found
