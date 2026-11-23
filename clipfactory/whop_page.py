"""Read a Whop campaign page from its link, the way anyone without an account sees it.

No login, no cookies, nothing sent to your Whop account: it is a plain page download, like opening the link in a
private browser window. When Whop shows the campaign (brief, rules, links to Google Docs / Drive / footage) the
text goes through the same reading as a pasted page. When Whop hides it behind a login, you're asked to paste it.
The page is read again every few hours, so a changed brief, new footage or the budget left is picked up.
"""
import hashlib
import json
import re
from html.parser import HTMLParser

import requests

from . import db

WHOP = re.compile(r"https?://(?:www\.)?whop\.com/\S+", re.I)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
WORDS = re.compile(r"\b(views?|per\s*1\s*k|cpm|clips?|campaign|budget|payout|rewards?)\b", re.I)
PASTE_HELP = ("Whop shows this campaign only after login, and I never log in to your Whop. Open it, press "
              "**Ctrl+A**, **Ctrl+C** and paste the text here (a long paste turns into a file, that's fine).")


class NeedsPaste(Exception):
    pass


class _Text(HTMLParser):
    """Visible text and every link of an HTML page; long strings inside the page's data scripts too."""
    SKIP = {"style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.scripts, self._skip, self._script = [], [], [], 0, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self._skip += 1
        if tag == "script":
            self._script = True
        if tag == "a" and (a.get("href") or "").startswith("http"):
            self.links.append(a["href"])
        if tag in ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "section", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "script":
            self._script = False

    def handle_data(self, data):
        if self._script:
            self.scripts.append(data)
        elif not self._skip and data.strip():
            self.parts.append(data.strip() + " ")


def _script_text(scripts):
    """Readable sentences hidden in the page's JSON (Next.js pages keep the brief there)."""
    out = []
    for s in scripts:
        if "__NEXT_DATA__" in s or s.lstrip().startswith("{"):
            try:
                stack = [json.loads(s[s.index("{"):])]
            except (ValueError, json.JSONDecodeError):
                stack = []
            while stack:
                x = stack.pop()
                if isinstance(x, dict):
                    stack += list(x.values())
                elif isinstance(x, list):
                    stack += x
                elif isinstance(x, str) and len(x) > 40 and " " in x:
                    out.append(x)
        for m in re.finditer(r'"((?:[^"\\]|\\.){60,4000})"', s):   # streamed page data: long quoted strings
            t = m.group(1)
            if " " in t and not t.startswith(("$", "http", "M0", "M1", "data:")):
                try:
                    out.append(json.loads(f'"{t}"'))
                except json.JSONDecodeError:
                    pass
    return list(dict.fromkeys(out))


def to_text(html):
    p = _Text()
    p.feed(html)
    text = re.sub(r"[ \t]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    extra = [t for t in _script_text(p.scripts) if t[:80] not in text]
    links = sorted(set(u for u in p.links + re.findall(r"https?://[^\s\"'<>\\]+", html)
                       if re.search(r"docs\.google|drive\.google|dropbox\.com|youtu|twitch\.tv|kick\.com", u)))
    out = text
    if extra:
        out += "\n\n" + "\n".join(extra[:200])
    if links:
        out += "\n\nLinks on the page:\n" + "\n".join(links[:40])
    return out


def fetch(url):
    """The campaign page's text (with its links). Raises NeedsPaste when Whop doesn't show it without login."""
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=30,
                         allow_redirects=True)
    except requests.RequestException as e:
        raise NeedsPaste(f"Couldn't reach Whop ({type(e).__name__}). {PASTE_HELP}") from None
    if r.status_code != 200 or "/login" in r.url or "text/html" not in r.headers.get("content-type", "text/html"):
        raise NeedsPaste(PASTE_HELP)
    text = to_text(r.text)
    if len(text) < 400 or len(WORDS.findall(text)) < 3:
        raise NeedsPaste(PASTE_HELP)
    return text[:60000]


def read_into(cid, url):
    """Read the page into the campaign (rules, guide, footage). Returns the reply for Discord."""
    from . import campaigns, joinflow
    text = fetch(url)
    c = campaigns.get(cid)
    extra = campaigns._extra(c)
    extra.update(whop_url=url, whop_hash=hashlib.sha1(text.encode()).hexdigest()[:16])
    db.x("UPDATE campaigns SET notes=? WHERE id=?", json.dumps(extra), cid)
    if not c["url"]:
        db.x("UPDATE campaigns SET url=? WHERE id=?", url, cid)
    return joinflow.apply_brief(cid, text + f"\n\nCampaign page: {url}")


def refresh(c):
    """Read a running campaign's page again; only a changed page is read into it again."""
    from . import campaigns, joinflow
    extra = campaigns._extra(c)
    url = extra.get("whop_url")
    if not url:
        return False
    try:
        text = fetch(url)
    except NeedsPaste:
        return False
    h = hashlib.sha1(text.encode()).hexdigest()[:16]
    if h == extra.get("whop_hash"):
        return False
    extra["whop_hash"] = h
    db.x("UPDATE campaigns SET notes=? WHERE id=?", json.dumps(extra), c["id"])
    db.log(c["id"], "The Whop campaign page changed: reading it again")
    joinflow.apply_brief(c["id"], text + f"\n\nCampaign page: {url}", quiet=True)
    return True
