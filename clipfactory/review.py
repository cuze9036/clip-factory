"""Stage 6: you approve or reject clips on Discord or the dashboard; 9+/10 clips approve themselves."""
import json


from . import db, notify as outbox, schedule
from .util import ET, iso, utcnow


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



def _clip(clip_id):
    return db.one("""SELECT c.*, m.hook, m.score, m.category, k.name campaign FROM clips c
                     JOIN moments m ON m.id=c.moment_id JOIN campaigns k ON k.id=c.campaign_id WHERE c.id=?""", clip_id)


def notify(clip_id):
    clip = _clip(clip_id)
    titles = json.loads(clip["titles"] or "[]")
    outbox.send("review", f"Clip #{clip_id} ready · {clip['campaign']} · {clip['score']:.0f}/10 {clip['category']}\n"
                + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
                + f"\nType `!approve {clip_id} 2` to pick title 2, or tap a button.",
                campaign_id=clip["campaign_id"], clip_id=clip_id, file_path=clip["final_path"],
                buttons=[[f"Post title {i + 1}", f"a:{clip_id}:{i}", "green"] for i in range(len(titles))]
                + [["Reject", f"r:{clip_id}", "red"]])


def auto_approve(clip_id):
    clip = _clip(clip_id)
    when = approve(clip_id, 0)
    if when:
        outbox.send("auto", f"Auto-posting clip #{clip_id} ({clip['score']:.0f}/10) · {clip['campaign']}\n"
                    f"{clip['title']}\nGoes live {when.astimezone(ET):%a %b %d, %I:%M %p} ET. Tap Cancel to stop it.",
                    campaign_id=clip["campaign_id"], clip_id=clip_id, file_path=clip["final_path"],
                    buttons=[["Cancel", f"x:{clip_id}", "red"]])
    return when


def cancel(clip_id):
    """Pull an approved clip before it posts. Posts already live stay up."""
    pending = db.q("SELECT * FROM posts WHERE clip_id=? AND status='queued'", clip_id)
    db.x("UPDATE posts SET status='failed', error='cancelled by you' WHERE clip_id=? AND status='queued'", clip_id)
    uploaded = db.one("SELECT COUNT(*) n FROM posts WHERE clip_id=? AND status='uploaded'", clip_id)["n"]
    if not db.one("SELECT 1 FROM posts WHERE clip_id=? AND status IN ('uploaded','posted')", clip_id):
        db.x("UPDATE clips SET status='rejected', reject_reason='cancelled' WHERE id=?", clip_id)
    return len(pending), uploaded
