"""Campaign guide: the creator's instructions (the campaign page, reference material, briefs) turned into short
"do" and "avoid" lines. The AI follows them when it picks moments and writes titles and captions."""
import json
import re

import requests

from . import campaigns, db, llm

DOC = re.compile(r"https?://docs\.google\.com/(document|presentation|spreadsheets)/d/([\w-]{20,})[^\s)>\]]*", re.I)
NOT_SHARED = ("That Google Doc isn't open to anyone with the link, so I can't read it. Either ask the campaign to "
              "set Share > General access > \"Anyone with the link\", or open it and use File > Download > "
              "Plain text (.txt), then send that file here.")

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


def fetch_doc(url):
    """Text of a Google Doc / Slides / Sheet shared with "anyone with the link" (no login). Raises with a clear
    message when it isn't shared that way."""
    m = DOC.search(url)
    if not m:
        raise ValueError("not a Google Docs link")
    kind, doc_id = m.group(1).lower(), m.group(2)
    export = {"document": f"https://docs.google.com/document/d/{doc_id}/export?format=txt",
              "presentation": f"https://docs.google.com/presentation/d/{doc_id}/export/txt",
              "spreadsheets": f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=csv"}[kind]
    try:
        r = requests.get(export, timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        raise RuntimeError(f"Couldn't reach Google Docs ({type(e).__name__})") from None
    if r.status_code != 200 or "accounts.google.com" in r.url or "text/html" in r.headers.get("content-type", ""):
        raise RuntimeError(NOT_SHARED)
    r.encoding = r.encoding or "utf-8"
    return r.text.lstrip("\ufeff").strip()


def expand(text):
    """The text with every Google Docs link in it replaced by the doc's own text. Returns (text, problems)."""
    problems, docs = [], []
    for m in list(DOC.finditer(text or ""))[:5]:
        try:
            docs.append(f"\n\n[Google Doc]\n{fetch_doc(m.group(0))[:20000]}")
        except Exception as e:
            problems.append(str(e))
    return (text or "") + "".join(docs), problems


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
    text, problems = expand(text)
    if problems and len(DOC.sub("", text).strip()) < 40:   # it was only a doc link, and it couldn't be read
        raise RuntimeError(problems[0])
    data, _ = llm.complete_json("campaign_guide", PROMPT.format(text=text[:40000]), _validate)
    extra = campaigns._extra(c)
    g = get(c)
    for k, cap in (("do", 15), ("avoid", 10)):
        g[k] = list(dict.fromkeys(g[k] + data[k]))[:cap]
    extra["guide"] = g
    old = extra.get("guide_text") or ""
    if text.strip() and text.strip()[:2000] not in old:   # the full text, for the campaign check
        extra["guide_text"] = (old + "\n\n" + text.strip())[-60000:].strip()
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
