"""Bugs the tool can't fix itself go to Claude Code as GitHub issues on the tool's own repository.

A scheduled Claude Code task reads open issues whose title starts with "[auto-report]", fixes the code, runs the
tests and pushes; the laptop installs the fix within a minute.

Needs GITHUB_ISSUES_TOKEN in .env: a fine-grained GitHub token for this one repository with only
"Issues: Read and write". The repository is public, so every report is scrubbed: no keys, tokens, links with
secrets, e-mail addresses or Windows user names.
"""
import hashlib
import re

import requests

from . import config, db
from .util import iso, utcnow

PREFIX = "[auto-report]"

SECRETS = [
    (re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.I), r"C:\\Users\\<user>"),
    (re.compile(r"/(home|Users)/[^/\s]+"), r"/\1/<user>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    (re.compile(r"\bEAA[A-Za-z0-9]{20,}"), "<meta-token>"),
    (re.compile(r"\b(sk|pk|ghp|gho|github_pat|xox[abp])[-_][A-Za-z0-9_-]{16,}"), "<token>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "<google-key>"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}"), "<telegram-token>"),
    (re.compile(r"https?://hc-ping\.com/\S+"), "<heartbeat-url>"),
    (re.compile(r"https?://discord(app)?\.com/api/webhooks/\S+"), "<discord-webhook>"),
    (re.compile(r"(rlkey|access_token|token|key|secret|sig)=[^&\s]+", re.I), r"\1=<hidden>"),
    (re.compile(r"\b[A-Za-z0-9_-]{40,}\b"), "<long-id>"),
]


def scrub(text):
    for rx, repl in SECRETS:
        text = rx.sub(repl, text or "")
    return text


def configured():
    return bool(config.env("GITHUB_ISSUES_TOKEN")) and bool(_repo())


def _repo():
    return (config.get().get("update") or {}).get("github_repo", "")


def _api(method, path, **kw):
    r = requests.request(method, f"https://api.github.com/repos/{_repo()}{path}", timeout=30,
                         headers={"Authorization": f"Bearer {config.env('GITHUB_ISSUES_TOKEN')}",
                                  "Accept": "application/vnd.github+json", "User-Agent": "clip-factory"}, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub answered HTTP {r.status_code}")
    return r.json()


def report(key, count, error, cause="", owner_fix=""):
    """Open an issue for this error (or add a short note to the open one, at most once a day).
    Returns the issue number, or None when reporting isn't set up."""
    if not configured():
        return None
    from .commands import version
    sig = hashlib.sha1(key.encode()).hexdigest()[:10]
    known = db.kv_get(f"bugreport:{sig}")
    today = utcnow().date().isoformat()
    body = scrub(f"**Error** ({count}x in 24 h, version {version()}):\n```\n{error[:1500]}\n```\n"
                 + (f"**Free-AI guess:** {cause}\n" if cause else "")
                 + (f"**Owner can try:** {owner_fix}\n" if owner_fix else "")
                 + f"\nSignature `{sig}` · reported {iso(utcnow())}")
    if known and known.get("open") and _api("GET", f"/issues/{known['number']}").get("state") == "closed":
        known = None   # fixed before, but it's back: a new report
    if known and known.get("open"):
        if known.get("noted") != today:
            _api("POST", f"/issues/{known['number']}/comments", json={"body": body})
            known["noted"] = today
            db.kv_set(f"bugreport:{sig}", known)
        return known["number"]
    issue = _api("POST", "/issues", json={"title": scrub(f"{PREFIX} {key}")[:200], "body": body})
    db.kv_set(f"bugreport:{sig}", {"number": issue["number"], "open": True, "noted": today})
    db.log(None, f"Bug reported to Claude Code: GitHub issue #{issue['number']}")
    return issue["number"]


def selftest_line():
    if not configured():
        raise RuntimeError("GITHUB_ISSUES_TOKEN not set")
    _api("GET", "")
    return f"can report bugs to {_repo()} (issues only)"
