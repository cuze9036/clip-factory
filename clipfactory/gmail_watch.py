"""Watches the Google Cloud project owner's Gmail for YouTube API audit emails and tells you on Discord.

Read-only: it uses only the gmail.readonly scope and never replies, deletes, archives or labels anything.
Discord only (never Telegram). If Gmail isn't connected, everything else keeps running.
"""
import base64
import html
import os
import re

from . import config, db, llm, notify
from .util import iso, utcnow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

DEFAULT_QUERY = 'from:google (youtube api OR "api compliance" OR quota OR audit) newer_than:7d'

SUMMARY_PROMPT = """Below is an email from Google about a YouTube API project (audit, compliance or quota).
Summarize it for the project owner in plain, simple English.

Return JSON only:
{{"summary": "2-3 short sentences: what Google says and what it wants",
  "action": "the exact thing the owner must do, or empty string if nothing",
  "deadline": "the deadline as written in the email, or empty string if none"}}

Subject: {subject}
Email:
{body}
"""


class NotConnected(Exception):
    pass


def _cfg():
    return config.section("gmail_watch")


def _account(name):
    acc = ((config.get().get("accounts") or {}).get("gmail") or {}).get(name) or {}
    return {"client_secrets": acc.get("client_secrets", "secrets/client_secret.json"),
            "token_file": acc.get("token_file", f"secrets/gmail_{name}.json")}


def token_path(account="main"):
    return config.path(_account(account)["token_file"])


def auth(account="main"):
    """One-time Google sign-in in the browser; saves a read-only token on this computer."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    acc = _account(account)
    flow = InstalledAppFlow.from_client_secrets_file(str(config.path(acc["client_secrets"])), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    token = token_path(account)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    os.chmod(token, 0o600)
    print("Gmail audit watcher connected (read-only). The token is saved in the secrets folder.")


def creds(account="main"):
    token = token_path(account)
    if not token.exists():
        raise NotConnected(f"not connected; run: clipfactory auth-gmail {account}")
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    c = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not c.valid and c.refresh_token:
        try:
            c.refresh(Request())
        except RefreshError:
            raise NotConnected(f"login expired; run: clipfactory auth-gmail {account}")
        token.write_text(c.to_json())
    if not c.valid:
        raise NotConnected(f"login not valid; run: clipfactory auth-gmail {account}")
    return c


def service(account="main"):
    c = creds(account)
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=c, cache_discovery=False)


# ---------- reading one email ----------

def _header(msg, name):
    for h in (msg.get("payload") or {}).get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _body(payload):
    """Plain text of an email: text/plain parts first, else text/html with the tags removed."""
    plain, rich = [], []

    def walk(part):
        mime, data = part.get("mimeType", ""), (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            rich.append(_decode(data))
        for p in part.get("parts") or []:
            walk(p)

    walk(payload or {})
    if plain:
        return "\n".join(plain)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", "\n".join(rich), flags=re.S | re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def _clean(text, limit=6000):
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _validate(d):
    if not isinstance(d, dict) or not str(d.get("summary") or "").strip():
        raise llm.Invalid("no summary")
    return {"summary": str(d["summary"]).strip()[:600], "action": str(d.get("action") or "").strip()[:300],
            "deadline": str(d.get("deadline") or "").strip()[:120]}


def summarize(subject, body):
    try:
        data, _ = llm.complete_json("gmail_summary", SUMMARY_PROMPT.format(subject=subject, body=_clean(body)),
                                    _validate)
        return data
    except llm.LLMError:
        return {"summary": _clean(body, 400), "action": "", "deadline": ""}


def message_text(row):
    """The Discord message for one email. Deadline / action required goes on top in bold."""
    top = []
    if row.get("deadline"):
        top.append(f"**Deadline: {row['deadline']}**")
    if row.get("action"):
        top.append(f"**Action required: {row['action']}**")
    lines = top + ["📧 Google audit email", f"Subject: {row['subject']}", f"Date: {row['date']}",
                   row["summary"]]
    return "\n".join(lines)


# ---------- the watcher ----------

def check(svc=None, account=None):
    """Look for new matching emails and post each new one to Discord once. Returns how many were new."""
    cfg = _cfg()
    account = account or cfg.get("account", "main")
    if svc is None:
        try:
            svc = service(account)
        except NotConnected:
            return 0
    query = cfg.get("query") or DEFAULT_QUERY
    res = svc.users().messages().list(userId="me", q=query, maxResults=int(cfg.get("max_per_run", 10))).execute()
    new = 0
    for ref in res.get("messages") or []:
        if db.one("SELECT 1 FROM gmail_seen WHERE id=?", ref["id"]):
            continue
        msg = svc.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        subject = _header(msg, "Subject") or "(no subject)"
        body = _body(msg.get("payload")) or msg.get("snippet", "")
        s = summarize(subject, body)
        row = {"id": ref["id"], "subject": subject[:200], "date": _header(msg, "Date")[:60], **s}
        db.x("INSERT OR IGNORE INTO gmail_seen(id, subject, date, internal_ms, summary, action, deadline, seen_at) "
             "VALUES(?,?,?,?,?,?,?,?)", row["id"], row["subject"], row["date"], int(msg.get("internalDate") or 0),
             row["summary"], row["action"], row["deadline"], iso(utcnow()))
        notify.send("audit", message_text(row))
        new += 1
    if new:
        db.log(None, f"Gmail audit watcher: {new} new email(s) posted to Discord")
    return new


def recent(days=7):
    cutoff_ms = int((utcnow().timestamp() - days * 86400) * 1000)
    return db.q("SELECT * FROM gmail_seen WHERE internal_ms >= ? ORDER BY internal_ms DESC", cutoff_ms)


def emails_command():
    """Reply for !emails: audit emails from the last 7 days."""
    connected = token_path(_cfg().get("account", "main")).exists()
    if connected:
        try:
            check()
        except Exception as e:
            db.log(None, f"Gmail audit watcher failed: {str(e)[:200]}", "warn")
    rows = recent(7)
    if not rows:
        return ("No Google audit emails in the last 7 days." if connected else
                "Gmail audit watcher is not connected. On the laptop run: `clipfactory auth-gmail main`")
    out = [f"**Google audit emails, last 7 days ({len(rows)})**"]
    for r in rows:
        flag = " · **action required**" if r["action"] or r["deadline"] else ""
        out.append(f"• {r['date'][:31]} · {r['subject'][:90]}{flag}\n  {r['summary'][:220]}")
    return "\n".join(out)[:1900]


def selftest_line():
    """For selftest: 'login valid' or raises NotConnected."""
    svc = service(_cfg().get("account", "main"))
    svc.users().getProfile(userId="me").execute()
    return "login valid"

