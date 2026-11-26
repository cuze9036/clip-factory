"""Quality check of every finished clip, before anyone sees it.

Gemini watches the finished clip with sound, the way a viewer would, and looks for what makes an edit look cheap:
our text on top of text that was already there, the same words twice, the @handle on other text, captions that
don't match the speech, a bad crop (cut faces, a webcam shown twice, a tiny picture), a start or end mid-sentence,
and anything the campaign's guide forbids. A clip that fails is fixed by itself (captions off, another style,
other timing or another moment) up to `qa.max_fixes` times; a clip that still fails goes to review with the
problems listed and is never auto-approved.
"""
import json

from . import config, db, llm

ISSUES = ("text_overlap", "double_text", "handle_on_text", "caption_mismatch", "bad_crop", "bad_start", "bad_end",
          "rule_break", "product_missing", "boring", "other")
FIXES = ("none", "captions_off", "other_style", "start_earlier", "start_later", "longer", "shorter", "other_moment")

PROMPT = """You are the strict quality checker of an automatic short-video editor. Watch this finished vertical
clip from start to end, with sound, like a viewer on TikTok / Reels / Shorts would.

What the editor put on top of the footage: {added}
Campaign: {name}
The campaign's guide (follow it): {guide}

Look for these problems:
- text_overlap: the editor's text (captions, hook text, emoji) covers or touches text that was already in the
  video (its own subtitles, a title, a game HUD, a sign)
- double_text: the same words are on screen twice at the same time (the video's own captions and ours)
- handle_on_text: the small @handle sits on top of other text
- caption_mismatch: our captions don't match what is said, or are hard to read
- bad_crop: faces or important text cut off, a streamer's webcam shown twice, black bars, a tiny picture
- bad_start / bad_end: it starts in the middle of a sentence or action, or ends cut off before the payoff
- rule_break: anything the guide forbids; product_missing: the guide wants a product on screen and it isn't
- boring: nothing happens that would stop a viewer scrolling

Return JSON only:
{{"score": 1-10 (10 = ready to post as it is, 7 = fine, 4 = clearly flawed),
  "issues": [{{"type": one of {issues}, "detail": "what and when, short", "severity": "high" or "low"}}],
  "fix": the single best automatic fix, one of {fixes} ("none" when it is fine),
  "summary": "one short sentence"}}"""


def _validate(d):
    if not isinstance(d, dict):
        raise llm.Invalid("not an object")
    try:
        score = max(1.0, min(10.0, float(d.get("score"))))
    except (TypeError, ValueError):
        raise llm.Invalid("score must be a number") from None
    issues = []
    for i in d.get("issues") or []:
        if isinstance(i, dict):
            t = str(i.get("type") or "other").lower()
            issues.append({"type": t if t in ISSUES else "other", "detail": str(i.get("detail") or "")[:200],
                           "severity": "high" if str(i.get("severity")).lower() == "high" else "low"})
    fix = str(d.get("fix") or "none").lower()
    return {"score": score, "issues": issues[:8], "fix": fix if fix in FIXES else "none",
            "summary": str(d.get("summary") or "")[:200]}


def added_text(style, captions, plan, credit):
    if (plan or {}).get("recipe"):   # a campaign format: its own lines, a GTA-style HUD, the brand's end card
        ln = plan.get("lines") or {}
        return (f"the campaign's format: hook \"{ln.get('hook', '')}\" with a game HUD (money, stars, minimap) "
                f"over the first seconds, then \"{ln.get('reveal', '')}\", then \"{ln.get('cta', '')}\"; the "
                "brand's end card (logo + website) blended over the last 3 seconds. No speech captions.")
    parts = [f"edit style {style}"]
    parts.append("animated captions of the speech" if captions else "no captions (the footage has its own)")
    if (plan or {}).get("hook"):
        parts.append(f"hook text \"{plan['hook'].get('text', '')}\" over the first seconds")
    if (plan or {}).get("emojis"):
        parts.append("a few emojis above the captions")
    if credit:
        parts.append(f"a small {credit} handle")
    return "; ".join(parts)


def check(path, duration, c, style, captions, plan, credit):
    """The verdict for one finished clip, or None when no AI can watch videos."""
    if not llm.can_watch_video():
        return None
    from . import guide
    prompt = PROMPT.format(added=added_text(style, captions, plan, credit), name=c["name"],
                           guide="; ".join(guide.lines(c))[:3000] or "none",
                           issues=", ".join(ISSUES), fixes=", ".join(FIXES))
    import os
    st = os.stat(path)   # the same path is rendered again after a fix: the file's size and time keep answers apart
    video = {"path": str(path), "start": 0, "end": max(1, int(duration + 1)), "fps": 2, "detail": True,
             "stamp": f"{st.st_size}:{st.st_mtime_ns}"}
    data, _ = llm.complete_json("clip_qa", prompt, _validate, video=video)
    return data


def passed(r):
    cfg = config.section("qa")
    return r is None or (r["score"] >= cfg.get("min_score", 7)
                         and not any(i["severity"] == "high" for i in r["issues"]))


def text(r):
    """One or two lines for Discord."""
    if not r:
        return ""
    ok = "✅" if passed(r) else "⚠️"
    probs = "; ".join(f"{i['type'].replace('_', ' ')}: {i['detail']}" for i in r["issues"][:3])
    return f"\n{ok} AI check {r['score']:.0f}/10" + (f" · {probs}" if probs else "") + \
        (f"\n{r['summary']}" if r.get("summary") else "")


def tries(moment_id):
    return db.kv_get(f"qa_tries:{moment_id}") or 0


def fix(clip, r):
    """Apply the fix the check asked for. Returns a short description, or None when nothing more is tried."""
    from . import reedit
    cfg = config.section("qa")
    m_id = clip["moment_id"]
    if tries(m_id) >= cfg.get("max_fixes", 2) or r["fix"] == "none":
        return None
    db.kv_set(f"qa_tries:{m_id}", tries(m_id) + 1)
    if r["fix"] == "captions_off":
        db.kv_set(f"captions_off:{m_id}", True)
        db.x("UPDATE clips SET status='downloaded', error=NULL WHERE id=?", clip["id"])
        return "edited again without our captions"
    if r["fix"] == "other_style":
        db.kv_set(f"restyle:{clip['id']}", clip.get("style") or "")
        db.x("UPDATE clips SET status='downloaded', error=NULL WHERE id=?", clip["id"])
        return "edited again in another style"
    if r["fix"] == "other_moment":
        reedit._drop(clip["id"], "AI check: weak moment")
        return "dropped; the next moment becomes a clip"
    shifts = {"start_earlier": (-reedit.STEP, 0), "start_later": (reedit.STEP, 0), "longer": (0, reedit.STEP),
              "shorter": (0, -reedit.STEP)}[r["fix"]]
    return "cut again " + reedit._recut(clip, *shifts)


def store(clip_id, r):
    db.x("UPDATE clips SET qa=? WHERE id=?", json.dumps(r) if r else None, clip_id)


def get(clip):
    try:
        return json.loads(clip.get("qa") or "null")
    except (TypeError, ValueError):
        return None

