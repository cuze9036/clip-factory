"""Stage 6: you approve or reject clips on Discord or the dashboard; 9+/10 clips approve themselves."""
import json


from . import db, notify as outbox, schedule
from .util import iso, utcnow


def approve(clip_id, title_index=0, title_override=None):
    clip = db.one("SELECT * FROM clips WHERE id=?", clip_id)
    if not clip or clip["status"] != "pending_review":
        return None
    titles = json.loads(clip["titles"] or "[]")
    title = (title_override or "").strip() or (titles[title_index] if title_index < len(titles) else clip["title"])
    db.x("UPDATE clips SET status='approved', title=?, reviewed_at=? WHERE id=?", title[:100], iso(utcnow()), clip_id)
    db.log(clip["campaign_id"], f"Clip {clip_id} approved: {title}")
    return schedule.schedule_clip(clip_id)


def reject(clip_id, reason="not good enough"):
    clip = db.one("SELECT * FROM clips WHERE id=?", clip_id)
    if not clip or clip["status"] != "pending_review":
        return
    db.x("UPDATE clips SET status='rejected', reject_reason=?, reviewed_at=? WHERE id=?", reason, iso(utcnow()), clip_id)
    db.log(clip["campaign_id"], f"Clip {clip_id} rejected: {reason}")



def _style(clip):
    from .styles import LABELS
    return f"\nStyle: {LABELS.get(clip.get('style') or '', 'older look')}" if clip.get("style") is not None else ""


def plan_text(clip_id):
    """Where and when an approved clip goes out, platform by platform."""
    from .publish import name, when_text
    rows = db.q("SELECT * FROM posts WHERE clip_id=? AND status IN ('queued','manual','uploaded') ORDER BY scheduled_at",
                clip_id)
    if not rows:
        return "Not scheduled anywhere yet."
    from . import config
    always = config.get().get("always_manual") or []
    return "\n".join(f"• {name(p['platform'])}: {when_text(p)} · "
                     + ("posts by itself" if p.get("mode") == "auto" and p["platform"] not in always
                        else "you post it (video comes to Telegram)") for p in rows)


def _clip(clip_id):
    return db.one("""SELECT c.*, m.hook, m.score, m.category, k.name campaign FROM clips c
                     JOIN moments m ON m.id=c.moment_id JOIN campaigns k ON k.id=c.campaign_id WHERE c.id=?""", clip_id)


def _qa(clip):
    from . import qa
    return qa.text(qa.get(clip))


def notify(clip_id):
    clip = _clip(clip_id)
    titles = json.loads(clip["titles"] or "[]")
    outbox.send("review", f"Clip #{clip_id} ready · {clip['campaign']} · {clip['score']:.0f}/10 {clip['category']}"
                + _style(clip) + _qa(clip) + "\n"
                + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
                + f"\nType `!approve {clip_id} 2` to pick title 2, or tap a button.",
                campaign_id=clip["campaign_id"], clip_id=clip_id, file_path=clip["final_path"],
                buttons=[[f"Post title {i + 1}", f"a:{clip_id}:{i}", "green"] for i in range(len(titles))]
                + [["🔁 Re-edit", f"re:{clip_id}", "grey"], ["Reject", f"r:{clip_id}", "red"]])


def auto_approve(clip_id):
    clip = _clip(clip_id)
    when = approve(clip_id, 0)
    if when:
        outbox.send("auto", f"✅ Auto-approved clip #{clip_id} ({clip['score']:.0f}/10) · {clip['campaign']}{_style(clip)}\n"
                    f"{clip['title']}{_qa(clip)}\n{plan_text(clip_id)}\nTap Cancel to stop it, or Re-edit to change it.",
                    campaign_id=clip["campaign_id"], clip_id=clip_id, file_path=clip["final_path"],
                    buttons=[["🔁 Re-edit", f"re:{clip_id}", "grey"], ["Cancel", f"x:{clip_id}", "red"]])
    return when


def cancel(clip_id):
    """Pull an approved clip before it posts. Posts already live stay up."""
    pending = db.q("SELECT * FROM posts WHERE clip_id=? AND status='queued'", clip_id)
    db.x("UPDATE posts SET status='failed', error='cancelled by you' WHERE clip_id=? AND status='queued'", clip_id)
    uploaded = db.one("SELECT COUNT(*) n FROM posts WHERE clip_id=? AND status='uploaded'", clip_id)["n"]
    if not db.one("SELECT 1 FROM posts WHERE clip_id=? AND status IN ('uploaded','posted')", clip_id):
        db.x("UPDATE clips SET status='rejected', reject_reason='cancelled' WHERE id=?", clip_id)
    return len(pending), uploaded
