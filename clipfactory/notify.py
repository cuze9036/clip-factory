"""Messages for you. Every alert goes into an outbox; the Discord bot sends them and handles the buttons."""
import json

from . import db
from .util import iso, utcnow


def send(kind, text, campaign_id=None, clip_id=None, post_id=None, file_path=None, buttons=None):
    """buttons: [[label, custom_id, style]] where style is 'green', 'red' or 'grey'."""
    return db.x("""INSERT INTO outbox(kind, campaign_id, clip_id, post_id, text, file_path, buttons, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""", kind, campaign_id, clip_id, post_id, text[:1900], file_path,
                json.dumps(buttons or []), iso(utcnow()))


def pending(limit=10):
    rows = db.q("SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT ?", limit)
    for r in rows:
        r["buttons"] = json.loads(r["buttons"] or "[]")
    return rows


def mark_sent(row_id, message_id=None, error=None):
    db.x("UPDATE outbox SET sent_at=?, message_id=?, error=? WHERE id=?", iso(utcnow()), message_id, error, row_id)
