"""Campaign check: before any video of a campaign is watched, the AI reads everything the campaign gave (the pasted
page, the guide docs from Drive, the rules) and decides whether this tool can really do it.

Order for every campaign:
1. Read the whole campaign (page + guide docs + rules) -> this check.
2. ✅ go / 🟡 partial: the videos are watched and clipped, following the guide.
   ⛔ no: nothing is clipped and the lane is paused; Discord says why. ▶ Resume runs it anyway.
3. Things only you can do (account name, profile picture, a new account, connecting it on Whop) are listed on
   Discord as your to-do.

The check is saved on the campaign and done again whenever the page, guide or rules change.
"""
import hashlib
import json

from . import campaigns, db, llm, notify

VERDICTS = {"go": "✅ We can do this", "partial": "🟡 We can do part of it", "no": "⛔ Not a fit for this tool"}
BLOCKERS = {"filming_or_voiceover": "Film new footage, show a face on camera or record a voice-over",
            "shot_by_shot_edit": "Do a shot-by-shot edit (replace given shots with provided files)",
            "non_english_text": "Burn captions or hook text on the video in a language other than English"}
MAX_TRIES = 3   # if the AI can't answer this many times, the lane goes on (with a warning) instead of waiting forever

CAN = """What this clipping tool CAN do by itself:
- Download footage from YouTube, Twitch, Google Drive folders, Dropbox share links, local folders.
- Watch whole videos with AI (any spoken language), find the best moments, cut 5-180 s vertical 9:16 clips.
- Edit: animated English captions (speech in other languages is translated), hook text, punch zooms, jump cuts,
  face-following crop, emoji, flash, whoosh, several caption styles; or no captions when the footage has its own.
- English titles, descriptions, required hashtags, credit lines, #Sponsored / paid-promotion disclosure.
- Post automatically to YouTube Shorts, Instagram Reels and Facebook; TikTok and X are sent to the owner to post.
- Schedule posts at US peak times, track views.

What it CANNOT do:
- Film anything new, show a face on camera, voice-over, talk to the camera, record gameplay, unbox products.
- Write a scripted ad or product review in the owner's own voice; draw original art or animation.
- Log in to Whop, submit clips on Whop, change account names or profile pictures, create accounts (the owner does
  these by hand).
- Follow a shot-by-shot editing job: replace given shots with provided substitute files, re-edit a video to an
  exact edit list, or keep a full video as it is apart from listed changes.
- Burn on-screen captions or hook text in a language other than English (it can leave its own captions out).
  Post titles, descriptions and hashtags CAN be written in the campaign's language (e.g. Arabic).
- Post more than about 10 YouTube uploads a day."""

PROMPT = """You are checking a clipping campaign before an automated clipping tool starts working on it.
Read EVERYTHING below (the campaign page, the guide documents and the rules) and decide if the tool can make clips
that follow this campaign. Translate anything not in English in your head; answer in plain English only.

{can}

Campaign: {name}
Platforms it pays for: {platforms}
Rules: {rules}
Guide (short form): {guide}

Footage links the tool could NOT open (the owner was told): {blocked}

Campaign page and guide documents:
{text}

Return JSON only:
{{"needs": {{"filming_or_voiceover": true/false (the clips must show new filming, a face on camera, a voice-over or
                                     recorded gameplay of our own),
            "shot_by_shot_edit": true/false (the job is to replace given shots with provided files, or re-edit
                                  videos to an exact edit list, instead of cutting the best moments),
            "non_english_text": true/false (text burned ON the video must be in a language other than English;
                                 the post's caption / title / hashtags in another language do NOT count)}},
  "verdict": "go" (the tool can make clips that follow the rules) | "partial" (some parts need the owner or some
             clips/platforms can't be done, but clips can be made) | "no" (the campaign's main deliverable needs
             things the tool can't do, like filming, voice-over, a shot-by-shot substitution edit, or captions in
             another language; manual posting or Whop submission alone is never a reason for "no"),
  "summary": "2 sentences: what the campaign wants and how the tool will do it",
  "can": ["up to 6 short lines: what the tool will do for it"],
  "cannot": ["up to 5 short lines: what the tool can't do here, or empty"],
  "account_rules": ["every rule about the posting account itself: its name, profile picture, new or existing
                     account, public account, followers, region; copy them exactly, or empty"],
  "owner_todo": ["up to 5 short lines: what the owner must do by hand before or after posting (for example: check
                  the account name and profile picture are not about the brand, connect the account on Whop,
                  submit each clip link on Whop), or empty"]}}
"""


def _texts(c):
    brief, docs = (c.get("brief") or "").strip(), (campaigns._extra(c).get("guide_text") or "").strip()
    if brief and brief[:2000] in docs:   # the page was also read into the guide: don't send it twice
        brief = ""
    return "\n\n".join(t for t in [brief, docs] if t)


def digest(c):
    from . import guide
    g = guide.get(c)
    return hashlib.sha1(json.dumps([_texts(c), c.get("rules") or [], g["do"], g["avoid"]]).encode()).hexdigest()[:16]


def needed(c):
    """Only campaigns that gave us something to read (a page or guide). Own lanes from campaigns.yaml don't."""
    from . import guide
    g = guide.get(c)
    return c.get("origin") != "yaml" and bool(_texts(c).strip() or g["do"] or g["avoid"])


def _lines(d, k, n):
    return [str(x).strip()[:160] for x in d.get(k) or [] if str(x).strip()][:n]


def _validate(d):
    if not isinstance(d, dict) or d.get("verdict") not in VERDICTS:
        raise llm.Invalid("verdict must be go, partial or no")
    out = {"verdict": d["verdict"], "summary": str(d.get("summary") or "").strip()[:400],
           "can": _lines(d, "can", 6), "cannot": _lines(d, "cannot", 5),
           "account_rules": _lines(d, "account_rules", 6), "owner_todo": _lines(d, "owner_todo", 5)}
    # A yes to a plain question is steadier than the AI's overall verdict (the free AI flips between go and partial
    # on the same page), so any blocker makes it "no", with the reason listed.
    needs = d.get("needs") if isinstance(d.get("needs"), dict) else {}
    hits = [why for key, why in BLOCKERS.items() if needs.get(key) is True]
    if hits:
        out["verdict"] = "no"
        out["cannot"] = (hits + [x for x in out["cannot"] if x not in hits])[:5]
    llm.require_english(" ".join([out["summary"]] + out["can"] + out["cannot"] + out["account_rules"]
                                 + out["owner_todo"]))
    return out


def get(c):
    return campaigns._extra(c).get("check")


def run(c):
    """Read the whole campaign and save the verdict. Returns the check."""
    from . import guide
    prompt = PROMPT.format(can=CAN, name=c["name"], platforms=", ".join(c.get("platforms") or []) or "?",
                           rules="; ".join(c.get("rules") or []) or "none listed",
                           guide="; ".join(guide.lines(c)) or "none",
                           blocked=", ".join(campaigns._extra(c).get("blocked_links") or []) or "none",
                           text=_texts(c)[:50000] or "(only the rules and guide above)")
    data, provider = llm.complete_json("campaign_check", prompt, _validate)
    data["digest"] = digest(c)
    c = campaigns.get(c["id"])
    extra = campaigns._extra(c)
    extra["check"] = data
    db.x("UPDATE campaigns SET notes=? WHERE id=?", json.dumps(extra), c["id"])
    db.kv_set("board_dirty", True)
    db.log(c["id"], f"Campaign check: {data['verdict']} ({provider})")
    if data["verdict"] == "no" and not c["paused"]:
        campaigns.set_paused(c["id"], True)
    notify.send("progress", report(c, data), campaign_id=c["id"])
    return data


def report(c, d):
    out = [f"🧾 **Campaign check: {c['name']}**", f"**{VERDICTS[d['verdict']]}**"]
    if d["summary"]:
        out.append(d["summary"])
    out += [f"✅ {x}" for x in d["can"]] + [f"⛔ {x}" for x in d["cannot"]]
    if d["account_rules"]:
        out.append("**Account rules** (check your accounts):")
        out += [f"• {x}" for x in d["account_rules"]]
    if d["owner_todo"]:
        out.append("**Your to-do:**")
        out += [f"☐ {x}" for x in d["owner_todo"]]
    if d["verdict"] == "no":
        out.append(f"The lane is paused, nothing will be clipped. To run it anyway tap ▶ Resume (or `!resume {c['id']}`).")
    else:
        out.append("Now the videos are watched and clipped, following the guide.")
    return "\n".join(out)[:1900]


def ready(c):
    """True when this campaign's videos may be watched now. Runs the check first if it is missing or out of date."""
    if not needed(c):
        return True
    d = get(c)
    if not d or d.get("digest") != digest(c):
        try:
            d = run(c)
            db.kv_set(f"check_fail:{c['id']}", None)
        except llm.LLMError as e:
            n = (db.kv_get(f"check_fail:{c['id']}") or 0) + 1
            db.kv_set(f"check_fail:{c['id']}", n)
            db.log(c["id"], f"Campaign check failed ({n}/{MAX_TRIES}): {str(e)[:150]}", "warn")
            return n >= MAX_TRIES   # don't wait forever on a broken AI
    if d["verdict"] == "no":
        return bool(campaigns._extra(campaigns.get(c["id"])).get("check_override") == d.get("digest"))
    return True


def override(cid):
    """▶ Resume on a campaign the check said no to: run it anyway (until the page or guide changes)."""
    c = campaigns.get(cid)
    d = get(c) if c else None
    if d and d["verdict"] == "no":
        extra = campaigns._extra(c)
        extra["check_override"] = d.get("digest")
        db.x("UPDATE campaigns SET notes=? WHERE id=?", json.dumps(extra), cid)


def card_line(c):
    d = get(c)
    if not d:
        return "Check: not done yet (it runs before the first video is watched)" if needed(c) else ""
    line = f"Check: {VERDICTS[d['verdict']]}"
    if d["owner_todo"]:
        line += f" · your to-do: {d['owner_todo'][0]}"
    return line[:300]
