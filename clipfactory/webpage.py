"""Any web page as text plus its links, for campaign reference material that isn't on Drive / Notion / Dropbox.

The explorer (gdrive.explore) opens the campaign's reference link, reads the whole page, then follows the links
that can lead to more instructions or footage, level by level, until nothing new opens: pages on the same site,
Notion / Docs / Drive / Dropbox / YouTube links, and links whose address or text says "reference", "assets",
"guide", "examples"…. Login / sign-up / pricing / legal pages, app stores and social media posts are never opened.
"""
import re
from urllib.parse import urljoin, urlparse

import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
NEVER = re.compile(r"log-?in|sign-?in|sign-?up|register|/join\b|pricing|checkout|/cart|privacy|terms|cookie|legal|"
                   r"careers|/jobs|unsubscribe|apps\.apple\.com|play\.google\.com|^mailto:|google\.com/search|"
                   r"/cdn-cgi/|\.(png|jpe?g|gif|webp|svg|css|js|ico|woff2?|ttf|xml)(\?|$)", re.I)
HINT = re.compile(r"referen|material|resource|asset|brief|guide|rules?\b|format|example|clip|footage|download|"
                  r"template|end.?card|media|kit|press|instruction|requirement|faq|how-?to|brand", re.I)
MAX_PAGES = 25   # web pages opened per explore (Notion / Drive / Docs have their own limits)


def site(url):
    host = urlparse(url).netloc.lower()
    return ".".join(host.split(".")[-2:]) if host else ""


def worth_opening(url, parent, depth):
    """Open this link while exploring? Same site as the page it was on, or it looks like instructions/assets."""
    from . import campaigns
    if not url.startswith("http") or NEVER.search(url) or campaigns.NOT_FOOTAGE.search(url):
        return False
    if site(url) in ("contentrewards.com", "crusercontent.com", "whop.com"):
        return False   # the campaign site itself: read by whop_page, other clippers' posts aren't material
    return (parent and site(url) == site(parent) and depth <= 4) or bool(HINT.search(url)) or depth <= 1


def read(url, timeout=30):
    """{"title", "text", "links"} of a web page; a headless browser (no login, no cookies) for JavaScript pages."""
    from . import whop_page
    title, text, links = "", "", []
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=timeout,
                         allow_redirects=True)
        if r.status_code == 200 and "html" in r.headers.get("content-type", "html"):
            p = whop_page._Text()
            p.feed(r.text)
            text = re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", "".join(p.parts))).strip()
            links = [urljoin(r.url, u) for u in p.links]
            m = re.search(r"<title[^>]*>([^<]+)", r.text, re.I)
            title = m.group(1).strip() if m else ""
            url = r.url
    except requests.RequestException:
        pass
    if len(text.split()) < 80:   # a JavaScript page: open it like a private browser window
        try:
            rendered = whop_page.render(url, all_links=True)
            body, _, tail = rendered.partition("\n\nLinks on the page:\n")
            text = body if len(body.split()) > len(text.split()) else text
            links += [u.strip() for u in tail.splitlines() if u.strip().startswith("http")]
        except Exception:
            pass
    links = [u.split("#")[0] for u in dict.fromkeys(links) if u.startswith("http")]
    return {"title": title or url, "text": f"[Web page: {title or url}]\n{text[:15000]}", "links": links,
            "url": url}
