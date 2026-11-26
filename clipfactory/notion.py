"""Public Notion pages (campaign briefs, "reference material" hubs): the whole page as text plus every link in it.

Notion pages load with JavaScript, so a plain download shows nothing. Pages shared publicly ("Share → Publish",
notion.site / notion.so / notion.com links) can be read through the same public endpoint the Notion website uses,
with no login: every block (headings, lists, toggles, callouts, tables of links, embeds, files) is read, nested
blocks too, and sub-pages come back as links so the explorer can open them as the next level.
"""
import re
from urllib.parse import quote

import requests

ID = re.compile(r"([0-9a-f]{32})|([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
HOST = re.compile(r"https?://(?:[\w-]+\.)*(notion\.so|notion\.site|notion\.com)/", re.I)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
TEXT_TYPES = {"text": "", "header": "# ", "sub_header": "## ", "sub_sub_header": "### ", "bulleted_list": "- ",
              "numbered_list": "1. ", "to_do": "[ ] ", "toggle": "▸ ", "quote": "> ", "callout": "💡 ",
              "code": "", "column_list": "", "column": ""}


def is_notion(url):
    return bool(HOST.search(url or ""))


def page_id(url):
    """The page's id from its link (the 32 hex characters at the end, with or without dashes)."""
    path = re.split(r"[?#]", url)[0]
    ids = ID.findall(path)
    if not ids:
        return None
    raw = (ids[-1][0] or ids[-1][1]).replace("-", "").lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def page_url(pid):
    return f"https://www.notion.so/{pid.replace('-', '')}"


def _post(endpoint, body):
    r = requests.post(f"https://www.notion.so/api/v3/{endpoint}", json=body, timeout=30,
                      headers={"User-Agent": UA, "Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"Notion said {r.status_code}: the page may not be public (Share → Publish)")
    return r.json()


def _value(rec):
    v = (rec or {}).get("value") or {}
    return v.get("value", v) if isinstance(v.get("value"), dict) else v


def _blocks(pid):
    """Every block of the page (and of its toggles / columns), by id."""
    blocks, cursor = {}, {"stack": []}
    for chunk in range(6):
        data = _post("loadPageChunk", {"pageId": pid, "limit": 100, "cursor": cursor, "chunkNumber": chunk,
                                       "verticalColumns": False})
        for bid, rec in ((data.get("recordMap") or {}).get("block") or {}).items():
            v = _value(rec)
            if v:
                blocks[bid] = v
        cursor = data.get("cursor") or {"stack": []}
        if not cursor.get("stack"):
            break
    # nested blocks the chunks didn't include (closed toggles, columns): ask for them by id
    for _ in range(4):
        missing = [k for b in list(blocks.values()) if b.get("type") != "page" or b.get("id") == pid
                   for k in b.get("content") or [] if k not in blocks]
        if not missing:
            break
        data = _post("syncRecordValues", {"requests": [{"pointer": {"table": "block", "id": k}, "version": -1}
                                                       for k in missing[:200]]})
        got = 0
        for bid, rec in ((data.get("recordMap") or {}).get("block") or {}).items():
            v = _value(rec)
            if v:
                blocks[bid] = v
                got += 1
        if not got:
            break
    return blocks


def _rich(prop, links):
    """Notion rich text → plain text; links and page mentions are collected."""
    out = []
    for part in prop or []:
        if not part:
            continue
        text = part[0] if isinstance(part[0], str) else ""
        for ann in (part[1] if len(part) > 1 else []) or []:
            if ann and ann[0] == "a" and len(ann) > 1 and str(ann[1]).startswith("http"):
                links.append(ann[1])
                text = f"{text} ({ann[1]})"
            elif ann and ann[0] == "p" and len(ann) > 1:
                links.append(page_url(ann[1]))
        out.append(text if text != "‣" else "")
    return "".join(out)


VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm", ".mkv")


def file_url(src, block):
    """A file uploaded to Notion ("attachment:…" or Notion's own storage) → a public download link."""
    return (f"https://www.notion.so/signed/{quote(str(src), safe='')}?table=block&id={block['id']}"
            f"&spaceId={block.get('space_id') or ''}")


def download(url, out):
    """Save one Notion file (a video the campaign gives you) to `out`."""
    with requests.get(url, stream=True, timeout=120, headers={"User-Agent": UA}) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for part in r.iter_content(1 << 20):
                f.write(part)
    return out


def read(url, max_chars=40000):
    """{"title", "text", "links", "files": [(name, download link)]} of a public Notion page. Sub-pages come back
    as links (the next level); files uploaded to the page (videos, end cards, zips) as download links."""
    pid = page_id(url)
    if not pid:
        raise RuntimeError("that Notion link has no page id")
    blocks = _blocks(pid)
    root = blocks.get(pid)
    if not root:
        raise RuntimeError("Notion didn't show this page: it may not be public (Share → Publish → copy the link)")
    links, lines, files = [], [], []
    title = _rich((root.get("properties") or {}).get("title"), links) or "Notion page"

    def walk(bid, depth):
        b = blocks.get(bid)
        if not b or depth > 12 or len("\n".join(lines)) > max_chars:
            return
        t, props, fmt = b.get("type"), b.get("properties") or {}, b.get("format") or {}
        indent = "  " * max(depth - 1, 0)
        if t == "page" and bid != pid:   # a sub-page: the next level, opened by the explorer
            name = _rich(props.get("title"), links) or "sub-page"
            lines.append(f"{indent}📄 {name}: {page_url(bid)}")
            links.append(page_url(bid))
            return
        if t == "alias":   # "link to page"
            target = (fmt.get("alias_pointer") or {}).get("id")
            if target:
                lines.append(f"{indent}📄 {page_url(target)}")
                links.append(page_url(target))
            return
        if t in ("bookmark", "embed", "video", "file", "pdf", "image", "audio", "drive", "figma", "tweet",
                 "gist", "maps", "codepen", "typeform", "loom"):
            raw = ((props.get("source") or [[None]])[0] or [None])[0]
            src = _rich(props.get("link"), []) or (raw if isinstance(raw, str) else "") or fmt.get("display_source")
            cap = _rich(props.get("caption"), links) or _rich(props.get("title"), links)
            uploaded = src and any(h in str(src) for h in ("attachment:", "secure.notion-static", "prod-files-secure",
                                                            "file.notion.so"))
            if uploaded:   # a file uploaded to the page: a public download link
                name = cap or str(src).split("?")[0].rsplit(":", 1)[-1].rsplit("/", 1)[-1] or t
                files.append((name, file_url(src, b)))
                lines.append(f"{indent}[{t} on Notion] {name}")
            elif src and str(src).startswith("http"):
                links.append(src)
                lines.append(f"{indent}[{t}] {cap + ': ' if cap else ''}{src}")
            elif cap:
                lines.append(f"{indent}[{t}] {cap}")
        elif t in ("collection_view", "collection_view_page"):
            lines.append(f"{indent}[a Notion database: open it on Notion if its rows matter]")
        elif t == "table_row":
            cells = [_rich(v, links) for v in props.values()]
            lines.append(indent + " | ".join(c for c in cells if c))
        elif t != "page":
            txt = _rich(props.get("title"), links)
            if txt.strip():
                lines.append(indent + TEXT_TYPES.get(t, "") + txt)
        for kid in b.get("content") or []:
            walk(kid, depth + 1 if bid != pid else 1)

    walk(pid, 0)
    links = [u for u in dict.fromkeys(links) if u.startswith("http")]
    return {"title": title, "text": f"[Notion: {title}]\n" + "\n".join(lines)[:max_chars], "links": links,
            "files": files}
