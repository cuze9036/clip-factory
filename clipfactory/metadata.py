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


def write(clip_text, moment, campaign, hint=""):
    rules = llm.rules_block(campaign)
    if hint:   # the owner's note from a re-edit
        rules += f"\n\nThe owner's feedback on the earlier version (follow it): {hint}"
    prompt = llm.METADATA_PROMPT.format(category=moment["category"], hook=moment["hook"], why=moment["why"],
                                        transcript=(clip_text or "(no speech)")[:1200], topic=campaign["topic"],
                                        audience=campaign["audience"], rules=rules)
    try:
        meta, _ = llm.complete_json("metadata", prompt, _validate)
    except llm.LLMError:
        hook = (moment["hook"] or "You won't believe this").strip().rstrip(".")
        meta = {"titles": [hook, f"{hook} (wait for it)", f"Nobody expected this: {hook.lower()}"],
                "description": moment["why"] or "", "hashtags": []}
    return finalize(meta, campaign)


DISCLOSURE = re.compile(r"^#?((?i:sponsored|ad|paidpartnership)|\w+Ad)$")   # #Sponsored, #ad, #NikeAd


def disclosure_tag(campaign, hashtags=()):
    """Paid campaigns (e.g. Whop Content Rewards) need a paid-promotion tag visible before "more": the campaign's
    own #<Brand>Ad if it asks for one, else #Sponsored. Unpaid lanes (your own channel) get none."""
    if not campaign or not (campaign.get("rate_per_1k") or 0) > 0:
        return ""
    for h in list(campaign.get("required_hashtags") or []) + list(hashtags or []):
        h = str(h).strip()
        if DISCLOSURE.match(h):
            return h if h.startswith("#") else "#" + h
    return config.section("metadata").get("paid_disclosure") or "#Sponsored"


def strip_tag(text, tag):
    """Text without `tag` (it goes on the first line instead), tidied up."""
    if not tag:
        return text or ""
    text = re.sub(rf"(?<![\w#]){re.escape(tag)}(?!\w)", "", text or "", flags=re.I)
    lines = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def finalize(meta, campaign):
    # The paid-promotion tag is not stored here: publishing puts it on the first line of every caption.
    tag = disclosure_tag(campaign, meta["hashtags"])
    tags = list(dict.fromkeys(list(campaign["required_hashtags"]) + meta["hashtags"]))
    if tag:
        tags = [t for t in tags if not DISCLOSURE.match(str(t))]
    tags = tags[:6]
    parts = [meta["description"]]
    if campaign["credit"]:
        parts.append(campaign["credit"])
    parts.append(" ".join(tags))
    return {"titles": meta["titles"], "description": "\n\n".join(p for p in parts if p), "hashtags": tags}


def store_fields(meta):
    return json.dumps(meta["titles"]), meta["titles"][0], meta["description"], json.dumps(meta["hashtags"])
