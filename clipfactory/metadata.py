"""Stage 5: English titles, description and hashtags, with each campaign's required credit and tags."""
import json
import re

from . import config, llm


def _validate(data):
    titles = [str(t).strip().strip('"')[:90] for t in data.get("titles", []) if str(t).strip()][:3]
    if not titles:
        raise llm.Invalid("no titles")
    desc = str(data.get("description", "")).strip()[:600]
    tags = [("#" + re.sub(r"[^A-Za-z0-9_]", "", str(h).lstrip("#")))
            for h in data.get("hashtags", []) if re.sub(r"[^A-Za-z0-9_]", "", str(h))][:5]
    llm.require_english(*titles, desc)
    return {"titles": titles, "description": desc, "hashtags": tags}


def write(clip_text, moment, campaign):
    prompt = llm.METADATA_PROMPT.format(category=moment["category"], hook=moment["hook"], why=moment["why"],
                                        transcript=(clip_text or "(no speech)")[:1200], topic=campaign["topic"],
                                        audience=campaign["audience"], rules=llm.rules_block(campaign))
    try:
        meta, _ = llm.complete_json("metadata", prompt, _validate)
    except llm.LLMError:
        hook = (moment["hook"] or "You won't believe this").strip().rstrip(".")
        meta = {"titles": [hook, f"{hook} (wait for it)", f"Nobody expected this: {hook.lower()}"],
                "description": moment["why"] or "", "hashtags": []}
    return finalize(meta, campaign)


DISCLOSURE = re.compile(r"^#?((?i:sponsored|ad|paidpartnership)|\w+Ad)$")   # #Sponsored, #ad, #NikeAd


def finalize(meta, campaign):
    required = list(campaign["required_hashtags"])
    # Paid campaigns (e.g. Whop Content Rewards) require a paid-promotion tag like #Sponsored or #<Brand>Ad.
    tag = config.section("metadata").get("paid_disclosure", "#Sponsored")
    if (campaign.get("rate_per_1k") or 0) > 0 and tag and not any(DISCLOSURE.match(str(h)) for h in required + meta["hashtags"]):
        required.insert(0, tag)
    tags = list(dict.fromkeys(required + meta["hashtags"]))[:6]
    parts = [meta["description"]]
    if campaign["credit"]:
        parts.append(campaign["credit"])
    parts.append(" ".join(tags))
    return {"titles": meta["titles"], "description": "\n\n".join(p for p in parts if p), "hashtags": tags}


def store_fields(meta):
    return json.dumps(meta["titles"]), meta["titles"][0], meta["description"], json.dumps(meta["hashtags"])
