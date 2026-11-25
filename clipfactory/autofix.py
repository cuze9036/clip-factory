"""Fixes the tool's own problems by itself, every few minutes, and says what it did in #ai-insights.

Safe fixes it makes (each item at most twice, so nothing loops forever):
- videos, moments and clips that failed or got stuck are retried
- Instagram/Facebook posts that can't go out automatically (key refused, upload failed) are switched to
  Telegram, so you can still post them by hand on time
- hand-posts you missed get one more reminder

Errors it can't fix (bugs in the code) are explained by the free AI (Gemini / Free LLM API, never Claude), with a
ready-made report you can send to Claude Code.
"""
import datetime as dt
from collections import defaultdict

from . import config, db, llm, notify
from .util import iso, parse_iso, utcnow

LOG_KEY = "autofix_log"
SKIP_ERRORS = ("not English", "too short")

DIAGNOSE = """Clip Factory is a Python tool on a Windows laptop (yt-dlp, FFmpeg with NVIDIA encoding, faster-whisper,
Gemini, a Discord bot, Telegram, Meta and YouTube APIs). It logged this error {count} time(s) in the last 24 hours:

{error}

Explain it for the owner in simple English. Return JSON only:
{{"cause": "1-2 sentences: what most likely went wrong",
  "owner_fix": "what the owner can do now (a Discord command like /selftest or !retry, a key to renew, a setting), or empty",
  "needs_code_fix": true or false}}
"""


def _cfg():
    return config.section("insights")


def _tries(kind, item_id):
    return (db.kv_get(f"autofix_tries:{kind}:{item_id}") or 0)


def _bump(kind, item_id):
    db.kv_set(f"autofix_tries:{kind}:{item_id}", _tries(kind, item_id) + 1)


def _ago(hours):
    return iso(utcnow() - dt.timedelta(hours=hours))


def _retry(table, status_sql, new_status, kind, extra=""):
    max_tries = _cfg().get("autofix_max_tries", 2)
    n = 0
    for r in db.q(f"SELECT id, error FROM {table} WHERE {status_sql} {extra}"):
        if any(s in (r["error"] or "") for s in SKIP_ERRORS) or _tries(kind, r["id"]) >= max_tries:
            continue
        _bump(kind, r["id"])
        db.x(f"UPDATE {table} SET status=?, error=NULL WHERE id=?", new_status, r["id"])
        n += 1
    return n


def _meta_ok(platform):
    from . import selftest
    r = selftest._check(platform, selftest._meta(platform))
    # Meta out of reach (no internet) says nothing about the key: only a refusal counts as a broken key
    return r.startswith("OK") or any(w in r for w in ("could not reach", "could not be reached", "Connection", "timed out", "Timeout"))


def _meta_ok_cached(platform, minutes=30):
    """Asks Meta at most every 30 minutes whether the platform's key works."""
    k = f"meta_ok:{platform}"
    got = db.kv_get(k)
    if got and utcnow() - parse_iso(got["at"]) < dt.timedelta(minutes=minutes):
        return got["ok"]
    ok = _meta_ok(platform)
    db.kv_set(k, {"at": iso(utcnow()), "ok": ok})
    return ok


def fix():
    """Make every safe fix. Returns a list of plain-English lines saying what was done."""
    done = []
    n = _retry("videos", "status='failed'", "new", "video", f"AND created_at >= '{_ago(72)}'")
    n += _retry("videos", "status='analyzing'", "new", "video", f"AND created_at < '{_ago(3)}'")
    if n:
        done.append(f"Retried {n} video(s) that failed or got stuck while being analysed")
    n = _retry("moments", "status='failed'", "found", "moment", f"AND created_at >= '{_ago(72)}'")
    if n:
        done.append(f"Retried {n} moment(s) whose download failed")
    n = _retry("clips", "status='failed' AND raw_path IS NOT NULL", "downloaded", "clip", f"AND created_at >= '{_ago(72)}'")
    if n:
        done.append(f"Retried {n} clip(s) whose editing failed")

    # Instagram / Facebook posts that can't go out by themselves: hand them to you instead of losing the slot.
    for platform in ("instagram", "facebook"):
        stuck = db.q(f"""SELECT id FROM posts WHERE platform=? AND mode='auto' AND (
                           (status='queued' AND scheduled_at < '{_ago(1)}') OR
                           (status='failed' AND scheduled_at >= '{_ago(24)}' AND COALESCE(error,'') NOT LIKE 'skipped%'))""",
                     platform)
        upcoming = db.q("SELECT id FROM posts WHERE platform=? AND mode='auto' AND status='queued'", platform)
        broken = bool(stuck or upcoming) and not _meta_ok_cached(platform)
        if broken:
            # the key is refused: every later post goes to Telegram now, so it arrives early instead of failing
            stuck = list({p["id"]: p for p in list(stuck) + list(upcoming)}.values())
        if not stuck:
            continue
        reason = "its key isn't working (see /selftest)" if broken else "the upload failed"
        new = 0
        for p in stuck:
            db.x("UPDATE posts SET mode='manual', status='queued', error=NULL WHERE id=?", p["id"])
            new += not db.kv_get(f"handed:{p['id']}")
            db.kv_set(f"handed:{p['id']}", True)   # stays with you while the key doesn't work
        if new:   # said once per post, not again each time a post is re-checked
            done.append(f"{new} {platform.title()} post(s) couldn't go out by themselves ({reason}), so they "
                        "come to you on Telegram now")

    # Hand-posts you missed: one more reminder.
    for p in db.q(f"SELECT * FROM posts WHERE status='manual' AND scheduled_at < '{_ago(2)}' AND scheduled_at >= '{_ago(24)}'"):
        if _tries("remind", p["id"]) >= 1:
            continue
        _bump("remind", p["id"])
        notify.send("manual", f"⏰ Reminder: post {p['id']} on {p['platform'].title()} wasn't posted yet. The video is "
                    f"on Telegram. Post it now and send `!posted {p['id']} <link>`, or tap Skip.",
                    post_id=p["id"], buttons=[["Skip this one", f"skip:{p['id']}", "grey"]])
        done.append(f"Reminded you again about post {p['id']} ({p['platform']})")
    return done


# ---------- errors it can't fix: explain them ----------

def _diag_validate(d):
    if not isinstance(d, dict) or not str(d.get("cause") or "").strip():
        raise llm.Invalid("no cause")
    out = {"cause": str(d["cause"]).strip()[:300], "owner_fix": str(d.get("owner_fix") or "").strip()[:200],
           "needs_code_fix": bool(d.get("needs_code_fix"))}
    llm.require_english(out["cause"])
    return out


def diagnose():
    """Each error seen 2+ times today, explained once a day by the free AI."""
    counts, sample = defaultdict(int), {}
    for e in db.q("SELECT msg FROM events WHERE level='error' AND at >= ?", _ago(24)):
        key = e["msg"].split(":")[0][:80]
        counts[key] += 1
        sample[key] = e["msg"][:600]
    out = []
    today = utcnow().date().isoformat()
    for key, n in sorted(counts.items(), key=lambda x: -x[1])[:3]:
        if n < 2 or db.kv_get(f"autofix_diag:{today}:{key}"):
            continue
        db.kv_set(f"autofix_diag:{today}:{key}", True)
        try:
            d, _ = llm.complete_json("diagnose", DIAGNOSE.format(count=n, error=sample[key]), _diag_validate)
        except llm.LLMError:
            d = {"cause": "", "owner_fix": "", "needs_code_fix": n >= 3}   # no AI answer: repeated errors still go to Claude Code
        lines = [f"🔍 **{key}** ×{n}"]
        if d["cause"]:
            lines.append(f"Why: {d['cause']}")
        if d["owner_fix"]:
            lines.append(f"You can: {d['owner_fix']}")
        if d["needs_code_fix"]:
            from . import bugreport
            try:
                number = bugreport.report(key, n, sample[key], d["cause"], d["owner_fix"])
            except Exception as e:
                number = None
                db.log(None, f"Couldn't report the bug to GitHub: {str(e)[:120]}", "warn")
            if number:
                lines.append(f"Needs a code fix: reported to Claude Code (GitHub issue #{number}). "
                             "The fix arrives on the laptop by itself.")
            else:
                from .commands import version
                lines.append("Needs a code fix. Send this to Claude Code:\n```\nClip Factory error (version "
                             f"{version()}), {n}x in 24 h:\n{sample[key]}\n```")
        out.append("\n".join(lines))
    return out


def recent_log(hours=24):
    cutoff = utcnow() - dt.timedelta(hours=hours)
    return [x["text"] for x in db.kv_get(LOG_KEY) or [] if parse_iso(x["at"]) >= cutoff]


def run():
    done = fix()
    notes = diagnose()
    db.kv_set("autofix_last_run", iso(utcnow()))
    if done:
        log = [x for x in db.kv_get(LOG_KEY) or [] if parse_iso(x["at"]) >= utcnow() - dt.timedelta(days=2)]
        db.kv_set(LOG_KEY, log + [{"at": iso(utcnow()), "text": t} for t in done])
        notify.send("health", "🛠️ **Fixed by itself**\n" + "\n".join(f"• {t}" for t in done))
        db.log(None, "Auto-fix: " + "; ".join(done))
    for n in notes:
        notify.send("health", n[:1900])
    return done, notes
