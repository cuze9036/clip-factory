"""🔁 Re-edit: you didn't like a clip, so you say what to change and the tool makes it again.

Quick buttons: new titles, start earlier / later, longer / shorter, a different moment.
Or type what you want ("start when the crowd screams, shorter title"): the free AI turns it into the same changes.
Clips that are already live can't be re-edited (delete them on the platform instead).
"""
import json

from . import campaigns, db, llm, notify

STEP = 3.0   # seconds per "earlier / later / longer / shorter" tap

OPS = {"titles": "✏️ New titles", "earlier": "⏪ Start 3 s earlier", "later": "⏩ Start 3 s later",
       "longer": "➕ 3 s longer", "shorter": "➖ 3 s shorter", "other": "🔀 Different moment"}

PROMPT = """The owner of a clipping tool didn't like a short clip and wrote what to change.
The clip is cut from {start:.0f}s to {end:.0f}s of a longer video ({length:.0f} seconds long).
Its titles were: {titles}

Owner's note: "{note}"

Turn the note into changes. Return JSON only:
{{"start_shift": seconds to move the start (negative = earlier, 0 = keep),
  "end_shift": seconds to move the end (positive = later, 0 = keep),
  "new_titles": true if the titles should be rewritten,
  "title_hint": "what the new titles should do differently, or empty",
  "different_moment": true only if the owner wants a completely different moment}}
"""


def _validate(d):
    if not isinstance(d, dict):
        raise llm.Invalid("not an object")
    try:
        out = {"start_shift": max(-30.0, min(30.0, float(d.get("start_shift") or 0))),
               "end_shift": max(-30.0, min(30.0, float(d.get("end_shift") or 0))),
               "new_titles": bool(d.get("new_titles")), "title_hint": str(d.get("title_hint") or "")[:200],
               "different_moment": bool(d.get("different_moment"))}
    except (TypeError, ValueError):
        raise llm.Invalid("shifts must be numbers")
    return out


def buttons(clip_id):
    return [[label, f"ed:{clip_id}:{op}", "grey"] for op, label in OPS.items()]


def _check(clip_id):
    clip = db.one("SELECT * FROM clips WHERE id=?", clip_id)
    if not clip:
        return None, f"No clip #{clip_id}."
    if db.one("SELECT 1 FROM posts WHERE clip_id=? AND status IN ('posted','uploaded')", clip_id):
        return None, f"Clip #{clip_id} is already live, so it can't be re-edited. Delete it on the platform if needed."
    return clip, None


def start(clip_id):
    """The 🔁 Re-edit button: show the choices and wait for a typed note."""
    from . import joinflow
    clip, err = _check(clip_id)
    if err:
        return err, []
    joinflow.expect(clip["campaign_id"], "reedit")
    db.kv_set("reedit_clip", clip_id)
    return (f"🔁 **Re-edit clip #{clip_id}**: tap what to change, or type it "
            "(for example: \"start when the crowd screams, shorter title\")."), buttons(clip_id)


def _stop_posts(clip_id):
    db.x("DELETE FROM posts WHERE clip_id=? AND status='queued'", clip_id)


def _drop(clip_id, reason):
    _stop_posts(clip_id)
    db.x("UPDATE clips SET status='rejected', reject_reason=? WHERE id=?", reason, clip_id)


def _recut(clip, start_shift, end_shift, hint=""):
    """New bounds for the moment; the clip is cut, edited and sent for review again."""
    m = db.one("SELECT * FROM moments WHERE id=?", clip["moment_id"])
    c = campaigns.get(clip["campaign_id"])
    v = db.one("SELECT duration_s FROM videos WHERE id=? AND campaign_id=?", m["video_id"], c["id"])
    length = (v or {}).get("duration_s") or 10 ** 6
    start = max(0.0, (m["start_s"] or 0) + start_shift)
    end = min(length, (m["end_s"] or 0) + end_shift)
    lo, hi = c["min_len"] or 5, (c["max_len"] or 60) + 15
    if end - start < lo:
        end = min(length, start + lo)
    if end - start > hi:
        end = start + hi
    _stop_posts(clip["id"])
    db.x("UPDATE clips SET status='rejected', reject_reason='re-edited' WHERE id=?", clip["id"])
    db.x("UPDATE moments SET start_s=?, end_s=?, status='found' WHERE id=?", round(start, 2), round(end, 2), m["id"])
    if hint:
        db.kv_set(f"title_hint:{m['id']}", hint)
    db.kv_set("watcher_now", True)
    return f"{start:.0f}s → {end:.0f}s ({end - start:.0f} s)"


def _new_titles(clip, hint):
    from . import metadata
    m = db.one("SELECT * FROM moments WHERE id=?", clip["moment_id"])
    c = campaigns.get(clip["campaign_id"])
    text = " ".join(w["w"] for w in json.loads(clip["words"] or "[]"))
    old = json.loads(clip["titles"] or "[]")
    meta = metadata.write(text, m, c, hint=hint or "Write clearly different titles from: " + " | ".join(old))
    titles, title, desc, tags = metadata.store_fields(meta)
    _stop_posts(clip["id"])
    db.x("UPDATE clips SET titles=?, title=?, description=?, hashtags=?, status='pending_review' WHERE id=?",
         titles, title, desc, tags, clip["id"])
    from . import review
    review.notify(clip["id"])


def apply(clip_id, op):
    """A quick button."""
    from . import joinflow
    clip, err = _check(clip_id)
    if err:
        return err
    joinflow.done_waiting()
    if op == "titles":
        _new_titles(clip, "")
        return f"Clip #{clip_id}: new titles are coming for review."
    if op == "other":
        _drop(clip_id, "re-edit: different moment")
        return f"Clip #{clip_id} dropped. The next moment from this video becomes a clip instead."
    shifts = {"earlier": (-STEP, 0), "later": (STEP, 0), "longer": (0, STEP), "shorter": (0, -STEP)}[op]
    span = _recut(clip, *shifts)
    return f"Clip #{clip_id} is being cut again ({span}). The new version comes here for review in a few minutes."


def apply_text(clip_id, note):
    """A typed note: the free AI turns it into changes."""
    from . import joinflow
    clip, err = _check(clip_id)
    if err:
        return err
    m = db.one("SELECT * FROM moments WHERE id=?", clip["moment_id"])
    v = db.one("SELECT duration_s FROM videos WHERE id=? AND campaign_id=?", m["video_id"], clip["campaign_id"])
    try:
        d, _ = llm.complete_json("reedit", PROMPT.format(start=m["start_s"] or 0, end=m["end_s"] or 0,
                                                       length=(v or {}).get("duration_s") or 0,
                                                       titles=clip["titles"] or "[]", note=note[:500]), _validate)
    except llm.LLMError as e:
        return f"Couldn't read that note ({str(e)[:120]}). Tap one of the buttons instead."
    joinflow.done_waiting()
    if d["different_moment"]:
        _drop(clip_id, f"re-edit: {note[:80]}")
        return f"Clip #{clip_id} dropped; the next moment becomes a clip instead."
    hint = d["title_hint"] if d["new_titles"] else ""
    if d["start_shift"] or d["end_shift"]:
        span = _recut(clip, d["start_shift"], d["end_shift"], hint or note[:200])
        return f"Clip #{clip_id} is being cut again ({span}), following your note. It comes back for review soon."
    _new_titles(clip, hint or note[:200])
    return f"Clip #{clip_id}: new titles following your note are coming for review."


def consume_hint(moment_id):
    """A title hint left by a re-edit, used once when the new version's titles are written."""
    key = f"title_hint:{moment_id}"
    hint = db.kv_get(key)
    if hint:
        db.kv_set(key, None)
    return hint or ""


def notify_buttons(clip_id):
    notify.send("review", f"🔁 Re-edit clip #{clip_id}: what should change?", clip_id=clip_id, buttons=buttons(clip_id))
