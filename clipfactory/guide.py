"""Campaign guide: the creator's instructions (the campaign page, reference material, briefs) turned into short
"do" and "avoid" lines. The AI follows them when it picks moments and writes titles and captions."""
import json

from . import campaigns, db, llm

PROMPT = """Below are a clipping campaign's instructions or reference material for clippers.
Turn them into short, clear instructions for an AI that picks moments from long videos and writes clip titles and
captions. Keep only what changes how clips should be made or posted. Plain English.

Return JSON only:
{{"do": ["up to 12 lines, max 15 words each: what good clips for this campaign show or do"],
  "avoid": ["up to 8 lines, max 15 words each: what is not allowed"],
  "hashtags": ["#tag"] (only tags the text says every post must have),
  "credit": "credit line or @tag the text says every post must have, or empty",
  "clip_length": [min_seconds, max_seconds] or null}}

Text:
{text}
"""


def _validate(d):
    if not isinstance(d, dict):
        raise llm.Invalid("not an object")
    out = {"do": [str(x).strip()[:120] for x in d.get("do") or [] if str(x).strip()][:12],
           "avoid": [str(x).strip()[:120] for x in d.get("avoid") or [] if str(x).strip()][:8],
           "hashtags": [("#" + str(h).lstrip("#")).strip() for h in d.get("hashtags") or [] if str(h).strip("# ")][:6],
           "credit": str(d.get("credit") or "").strip()[:120], "clip_length": d.get("clip_length")}
    if not out["do"] and not out["avoid"]:
        raise llm.Invalid("no instructions found")
    llm.require_english(" ".join(out["do"] + out["avoid"]))
    return out


def get(c):
    g = campaigns._extra(c).get("guide") or {}
    return {"do": g.get("do") or [], "avoid": g.get("avoid") or []}


def lines(c):
    """For the AI prompts."""
    g = get(c)
    return [f"Do: {x}" for x in g["do"]] + [f"Avoid: {x}" for x in g["avoid"]]


def add(cid, text):
    """Read instructions into the campaign's guide (merged with what's there). Returns a short reply."""
    c = campaigns.get(cid)
    if not c:
        return f"No campaign `{cid}`."
    data, _ = llm.complete_json("campaign_guide", PROMPT.format(text=text[:20000]), _validate)
    extra = campaigns._extra(c)
    g = get(c)
    for k, cap in (("do", 15), ("avoid", 10)):
        g[k] = list(dict.fromkeys(g[k] + data[k]))[:cap]
    extra["guide"] = g
    db.x("UPDATE campaigns SET notes=? WHERE id=?", json.dumps(extra), cid)
    tags = list(dict.fromkeys(c["required_hashtags"] + data["hashtags"]))
    if tags != c["required_hashtags"]:
        db.x("UPDATE campaigns SET required_hashtags=? WHERE id=?", json.dumps(tags), cid)
    if data["credit"] and not c["credit"]:
        db.x("UPDATE campaigns SET credit=? WHERE id=?", data["credit"], cid)
    lo_hi = data["clip_length"]
    if isinstance(lo_hi, list) and len(lo_hi) == 2 and all(isinstance(x, (int, float)) for x in lo_hi) \
            and 5 <= lo_hi[0] < lo_hi[1] <= 180:
        db.x("UPDATE campaigns SET min_len=?, max_len=? WHERE id=?", lo_hi[0], lo_hi[1], cid)
    db.log(cid, f"Guide updated: {len(g['do'])} do, {len(g['avoid'])} avoid")
    return show(cid)


def show(cid):
    c = campaigns.get(cid)
    if not c:
        return f"No campaign `{cid}`."
    g = get(c)
    if not g["do"] and not g["avoid"]:
        return (f"**{c['name']}** has no guide yet. Paste the campaign's reference material or instructions with "
                f"`!guide {cid} <text>` (a long paste can be a file).")
    out = [f"**Guide for {c['name']}** (the AI follows this when it picks moments and writes titles)"]
    out += [f"✅ {x}" for x in g["do"]] + [f"⛔ {x}" for x in g["avoid"]]
    extras = []
    if c["required_hashtags"]:
        extras.append("tags " + " ".join(c["required_hashtags"]))
    if c["credit"]:
        extras.append("credit " + c["credit"])
    extras.append(f"clips {c['min_len']:.0f}-{c['max_len']:.0f} s")
    out.append("Every clip: " + " · ".join(extras))
    return "\n".join(out)[:1900]
