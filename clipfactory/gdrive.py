"""Google Drive footage and guides, from one link.

Give any Drive link (a folder, a file, or a Google Doc with the guide) as a campaign's footage. The bot explores
it by itself: it reads the guide documents, follows the links written inside them (the videos folder, another
Drive, Dropbox, YouTube), adds every video folder as footage and turns all the instructions into the campaign
guide. New videos in those folders are downloaded every few hours. It only reads; nothing in Drive is changed.

Access: the owner logs in once with `clipfactory auth-drive main` (read-only Drive permission, the Google account
that joined the campaign), so private folders shared with that account work. Without that login only
public ("anyone with the link") Google Docs can be read.
"""
import datetime as dt
import hashlib
import json
import os
import re

import requests

from . import campaigns, config, db
from .util import iso, parse_iso, utcnow

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
API = "https://www.googleapis.com/drive/v3"
FOLDER = re.compile(r"drive\.google\.com/(?:drive/(?:u/\d+/)?folders/|folderview\?id=|embeddedfolderview\?id=)([\w-]{10,})")
FILE = re.compile(r"drive\.google\.com/(?:file/(?:u/\d+/)?d/|open\?id=|uc\?(?:export=\w+&)?id=)([\w-]{10,})")
DOC = re.compile(r"docs\.google\.com/(document|presentation|spreadsheets)/(?:u/\d+/)?d/([\w-]{20,})")
# Places the bot can't download from: you'd have to fetch these yourself.
CANT_OPEN = re.compile(r"https?://(?:[\w-]+\.)*(mega\.nz|wetransfer\.com|we\.tl|mediafire\.com|frame\.io|box\.com|"
                       r"onedrive\.live\.com|1drv\.ms|sharepoint\.com|icloud\.com|"
                       r"terabox\.com|pcloud\.com|drop\.download|gofile\.io)/\S*", re.I)
# Folders / files a campaign gives you to put INTO your clips (end card, logo, examples to copy), not to clip from.
ASSET = re.compile(r"example|end ?card|outro|intro|logo|watermark|headline|thumbnail|overlay|template|tutorial|"
                   r"how ?to|music|sound|font|cta", re.I)
LINK = re.compile(r"https?://[^\s<>\"')\]]+")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
DOC_TYPES = {"application/vnd.google-apps.document": "text/plain",
             "application/vnd.google-apps.presentation": "text/plain",
             "application/vnd.google-apps.spreadsheet": "text/csv"}
LOGIN_HELP = ("This Drive isn't open to everyone, so I need read-only access once. On the laptop run:\n"
              "`clipfactory auth-drive main` and log in with the Google account that joined the campaign "
              "(Google Drive API must be enabled in the Google Cloud project). Then send the link again.")


class NeedsLogin(Exception):
    pass


# ---------- login ----------

def token_path(account="main"):
    return config.path(f"secrets/drive_{account}.json")


def auth(account="main"):
    from google_auth_oauthlib.flow import InstalledAppFlow
    secrets = config.path(((config.get().get("accounts") or {}).get("youtube") or {}).get("main", {})
                          .get("client_secrets", "secrets/client_secret.json"))
    creds = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES).run_local_server(port=0, open_browser=True)
    token = token_path(account)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    os.chmod(token, 0o600)
    print("Google Drive connected (read-only).")


def _creds(account="main"):
    token = token_path(account)
    if not token.exists():
        return None
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    c = Credentials.from_authorized_user_file(str(token), SCOPES)
    if not c.valid and c.refresh_token:
        try:
            c.refresh(Request())
            token.write_text(c.to_json())
        except Exception:
            return None
    return c if c.valid else None


# ---------- the Drive API ----------

class Drive:
    def __init__(self, account="main"):
        self.creds = _creds(account)
        self.key = config.env("GOOGLE_API_KEY")

    @property
    def ready(self):
        return bool(self.creds or self.key)

    def _get(self, path, params=None, stream=False):
        if not self.ready:
            raise NeedsLogin(LOGIN_HELP)
        params = dict(params or {}, supportsAllDrives="true")
        headers = {}
        if self.creds:
            headers["Authorization"] = f"Bearer {self.creds.token}"
        else:
            params["key"] = self.key
        try:
            r = requests.get(API + path, params=params, headers=headers, stream=stream, timeout=(20, 60))
        except requests.RequestException as e:
            raise RuntimeError(f"Google Drive couldn't be reached ({type(e).__name__})") from None
        if r.status_code in (401, 403, 404):
            raise NeedsLogin(LOGIN_HELP if not self.creds else
                             "Google Drive says this account can't open it (not shared with you yet, or "
                             "the Drive API isn't enabled).")
        if r.status_code >= 400:
            raise RuntimeError(f"Google Drive answered HTTP {r.status_code}")
        return r

    def meta(self, file_id):
        return self._get(f"/files/{file_id}", {"fields": "id,name,mimeType,size"}).json()

    def children(self, folder_id):
        out, page = [], None
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed=false", "pageSize": 200,
                      "fields": "nextPageToken,files(id,name,mimeType,size)", "includeItemsFromAllDrives": "true"}
            if page:
                params["pageToken"] = page
            data = self._get("/files", params).json()
            out += data.get("files", [])
            page = data.get("nextPageToken")
            if not page:
                return out

    def text(self, item):
        """Text of a Google Doc/Slides/Sheet, or of a plain text file."""
        mime = item.get("mimeType", "")
        if mime in DOC_TYPES:
            return self._get(f"/files/{item['id']}/export", {"mimeType": DOC_TYPES[mime]}).text
        if mime.startswith("text/") and int(item.get("size") or 0) < 2_000_000:
            return self._get(f"/files/{item['id']}", {"alt": "media"}).text
        return ""

    def download(self, file_id, dest, busy=None):
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self._get(f"/files/{file_id}", {"alt": "media"}, stream=True) as r:
            with open(tmp, "wb") as f:
                for n, chunk in enumerate(r.iter_content(1024 * 1024)):
                    if busy and n % 200 == 0:
                        busy.write_text(iso(utcnow()))
                    f.write(chunk)
        tmp.replace(dest)


def is_video(item):
    return item.get("mimeType", "").startswith("video/") or item.get("name", "").lower().endswith(VIDEO_EXT)


def is_folder(item):
    return item.get("mimeType") == "application/vnd.google-apps.folder"


# ---------- exploring one link ----------

def simple_help(url, err=""):
    """What went wrong with a link and what you can do, in one plain line."""
    e, u = (err or "").lower(), url.lower()
    if "drive.google" in u or "docs.google" in u:
        return ("it's private. Ask the campaign to share it with \"Anyone with the link\", or if your Google "
                "account can open it, run `clipfactory auth-drive main` once on the laptop.")
    if "twitch.tv" in u or "youtu" in u or "kick.com" in u:
        return ("the video doesn't open for me: it may be private, deleted, subscriber/members-only or blocked in "
                "some countries. Open it in a private browser window; if it doesn't play there, ask for another link.")
    if "dropbox" in u:
        return "the Dropbox link isn't public or has expired. Ask the campaign for a new share link."
    m = CANT_OPEN.search(url)
    if m:
        return (f"I can't download from {m.group(1)}. Download the videos yourself, put them in a Google Drive folder "
                "shared with \"Anyone with the link\", and send that link with 🎬 Footage.")
    if "login" in e or "401" in e or "403" in e or "permission" in e:
        return "it needs a login I don't have. Check it opens without logging in, or send another link."
    return "I couldn't open it. Check it opens without logging in, or send another link."


def blocked_text(name, blocked):
    """The Discord note for links that couldn't be opened: link -> what to do."""
    out = [f"🔒 **{name}: I couldn't open {len(blocked)} link(s).** What to do:"]
    out += [f"• <{u[:150]}>: {h}" for u, h in blocked[:8]]
    out.append("Everything else is used. When it's fixed, send the new link with 🎬 Footage.")
    return "\n".join(out)


def explore(cid, link, max_items=150, max_depth=8, drive=None):
    """Follow a link level by level until nothing new opens: Drive folders and Docs, public Notion pages (and
    their sub-pages and uploaded videos), and any web page with the links on it that can lead to instructions or
    footage. Each page is read whole; its text becomes the campaign guide, its videos the footage. Returns a reply."""
    from . import guide, notion, webpage
    drive = drive or Drive()
    c = campaigns.get(cid)
    queue, seen = [(link.strip().strip("<>"), 0)], set()
    folders, files, texts, other, problems, needs_login = {}, {}, [], [], [], False
    blocked = []   # (link, what to do): links the bot can't open, explained simply
    parents, read_pages, notion_videos, assets = {}, [], [], {}
    count = web = 0

    def take_links(text, depth, parent=None, more=()):
        for u in list(LINK.findall(text or "")) + list(more):
            u = u.rstrip(".,;")
            if CANT_OPEN.search(u):
                if u not in [b[0] for b in blocked]:
                    blocked.append((u, simple_help(u)))
                continue
            if any(rx.search(u) for rx in (FOLDER, FILE, DOC)) or notion.is_notion(u) or re.search(
                    r"dropbox\.com/|youtube\.com/|youtu\.be/|twitch\.tv/|kick\.com/|vimeo\.com/|streamable\.com/|"
                    r"\.(mp4|mov|m4v|webm|mkv)(\?|$)", u, re.I):
                queue.append((u, depth + 1))
            elif webpage.worth_opening(u, parent, depth + 1):
                parents.setdefault(u, parent)
                queue.append((u, depth + 1))

    while queue and count < max_items:
        url, depth = queue.pop(0)
        key = (FOLDER.search(url) or FILE.search(url) or DOC.search(url))
        ident = key.group(key.lastindex) if key else (notion.page_id(url) if notion.is_notion(url) else None) or \
            url.split("#")[0].rstrip("/")
        if ident in seen or depth > max_depth:
            continue
        seen.add(ident)
        count += 1
        try:
            if FOLDER.search(url):
                fid = FOLDER.search(url).group(1)
                kids = drive.children(fid)
                try:
                    name = drive.meta(fid).get("name") or fid
                except Exception:
                    name = fid
                videos = [k for k in kids if is_video(k)]
                if videos and ASSET.search(name):   # an end card / examples folder: kept, never clipped
                    assets[name] = f"https://drive.google.com/drive/folders/{fid}"
                elif videos:
                    folders[fid] = {"name": name, "videos": len(videos)}
                for k in kids:
                    if is_folder(k):
                        queue.append((f"https://drive.google.com/drive/folders/{k['id']}", depth + 1))
                    elif k.get("mimeType") in DOC_TYPES or k.get("mimeType", "").startswith("text/"):
                        t = drive.text(k)
                        if t.strip():
                            texts.append(f"[{k.get('name', 'guide')}]\n{t}")
                            take_links(t, depth)
                    elif k.get("mimeType") == "application/pdf":
                        other.append(f"{k.get('name')} (PDF: open it and paste its text with 📋 Guide)")
            elif DOC.search(url):
                did = DOC.search(url).group(2)
                if drive.ready:
                    item = drive.meta(did)
                    t = drive.text(item)
                else:
                    t = guide.fetch_doc(url)
                if t.strip():
                    texts.append(t)
                    take_links(t, depth)
            elif FILE.search(url):
                item = drive.meta(FILE.search(url).group(1))
                if is_video(item) and ASSET.search(item.get("name") or ""):
                    assets[item.get("name")] = f"https://drive.google.com/file/d/{item['id']}/view"
                elif is_video(item):
                    files[item["id"]] = item.get("name", item["id"])
                elif is_folder(item):
                    queue.append((f"https://drive.google.com/drive/folders/{item['id']}", depth))
                else:
                    t = drive.text(item)
                    if t.strip():
                        texts.append(t)
                        take_links(t, depth)
            elif notion.is_notion(url):   # a public Notion page: all of it, sub-pages are the next level
                page = notion.read(url)
                texts.append(page["text"])
                read_pages.append(page["title"])
                for name, link in page["files"]:
                    if name.lower().endswith(notion.VIDEO_EXT) and ASSET.search(name):
                        assets[name] = link
                    elif name.lower().endswith(notion.VIDEO_EXT):
                        notion_videos.append((name, link))
                    else:
                        other.append(f"{name} (a file on the Notion page)")
                take_links("", depth, url, page["links"])
            else:   # Dropbox / YouTube / Twitch links found inside a guide, or any other web page
                try:
                    other.append(campaigns.add_source(cid, url))
                except ValueError:
                    if re.search(r"dropbox\.com/|twitch\.tv/\w+/videos?/|/videos/", url):
                        blocked.append((url, simple_help(url)))
                    elif web < webpage.MAX_PAGES and (depth == 0 or webpage.worth_opening(url, parents.get(url), depth)):
                        web += 1   # a web page: read it whole and follow its links one level deeper
                        page = webpage.read(url)
                        if len(page["text"].split()) > 20:
                            texts.append(page["text"])
                            read_pages.append(page["title"][:60])
                        take_links("", depth, page["url"], page["links"])
        except NeedsLogin as e:
            needs_login = True
            problems.append(str(e))
            blocked.append((url, simple_help(url, str(e))))
        except Exception as e:
            problems.append(f"{url[:80]}: {str(e)[:120]}")
            blocked.append((url, simple_help(url, str(e))))

    if notion_videos:   # videos uploaded to the Notion page: saved in a folder the watcher clips from
        d = config.data_dir() / "footage" / cid / "notion"
        d.mkdir(parents=True, exist_ok=True)
        got = 0
        for name, link in notion_videos[:30]:
            out = d / re.sub(r'[<>:"/\\|?*]+', "_", name)
            try:
                if not out.exists():
                    notion.download(link, out)
                got += 1
            except Exception as e:
                problems.append(f"{name}: {str(e)[:80]}")
        if got:
            try:
                campaigns.add_source(cid, str(d))
            except Exception:
                pass   # already a source
            other.append(f"{got} video(s) from the Notion page saved as footage")
    for fid in folders:
        _add_source(cid, f"folder:{fid}")
    for fid in files:
        _add_source(cid, f"file:{fid}")
    out = [f"**{c['name']}**: explored the link ({count} link(s) opened, level by level)."]
    if read_pages:
        out.append(f"📖 Read {len(read_pages)} page(s): " + ", ".join(dict.fromkeys(read_pages))[:400])
    if folders or files:
        n = sum(f["videos"] for f in folders.values()) + len(files)
        out.append(f"🎬 Footage: {n} video(s) in {len(folders)} folder(s)" + (f" + {len(files)} file(s)" if files else "")
                   + ". They download now; new ones are picked up every few hours.")
    if texts:
        try:
            guide.add(cid, "\n\n".join(texts)[:40000], expand_docs=False)   # every doc was already opened here
            g = guide.get(campaigns.get(cid))
            out.append(f"📋 Guide read from {len(texts)} document(s): {len(g['do'])} things to do, "
                       f"{len(g['avoid'])} to avoid (see `!guide {cid}`).")
        except Exception as e:
            out.append(f"Couldn't turn the documents into a guide: {str(e)[:120]}")
    out += [f"• {o}" for o in other[:6]]
    if needs_login:
        out.append(LOGIN_HELP)
    if blocked:
        from . import notify
        notify.send("progress", blocked_text(c["name"], blocked), campaign_id=cid)
        out.append(f"🔒 {len(blocked)} link(s) I couldn't open: see the note below for what to do.")
    if assets:
        out.append("🧩 Assets to put into clips (not clipped as footage): " + ", ".join(list(assets)[:8]))
    extra = campaigns._extra(campaigns.get(cid))
    if assets:
        extra["assets"] = {**(extra.get("assets") or {}), **assets}
    extra["blocked_links"] = list(dict.fromkeys((extra.get("blocked_links") or []) + [u for u, _ in blocked]))[-20:]
    db.x("UPDATE campaigns SET notes=? WHERE id=?", json.dumps(extra), cid)
    if not (folders or files or texts or other) and not problems:
        out.append("No videos or guides found behind that link.")
    c = campaigns.get(cid)
    if (folders or files or notion_videos) and c.get("stage") == "suggested" and campaigns._extra(c).get("joined_at"):
        campaigns.activate(cid)
        out.append(f"**{c['name']} is running.** Clips will show up here for review.")
    return "\n".join(out)


def _add_source(cid, ident):
    db.x("""INSERT INTO sources(campaign_id, kind, name, channel_id, streams_only, layout) VALUES(?,?,?,?,?,?)
            ON CONFLICT(campaign_id, channel_id) DO NOTHING""", cid, "gdrive", f"Google Drive {ident}", ident, 0, "blur")
    db.kv_set("watcher_now", True)


# ---------- the watcher: download new videos ----------

def sync(c, s, root, drive=None):
    """Download videos from a Drive folder (and its subfolders) or file into `root` that aren't there yet."""
    drive = drive or Drive()
    root.mkdir(parents=True, exist_ok=True)
    busy = config.data_dir() / "busy.lock"
    kind, _, ident = s["channel_id"].partition(":")
    items = []
    if kind == "file":
        items = [drive.meta(ident)]
    else:
        stack = [(ident, 0)]
        while stack:
            fid, depth = stack.pop()
            for k in drive.children(fid):
                if is_video(k):
                    items.append(k)
                elif is_folder(k) and depth < 3:
                    stack.append((k["id"], depth + 1))
    new = 0
    try:
        for it in items:
            if not is_video(it):
                continue
            safe = re.sub(r'[<>:"/\\|?*]+', "_", it.get("name") or it["id"])
            dest = root / f"{hashlib.sha1(it['id'].encode()).hexdigest()[:6]}_{safe}"
            if not dest.suffix:
                dest = dest.with_suffix(".mp4")
            if dest.exists() and (not it.get("size") or dest.stat().st_size == int(it["size"])):
                continue
            busy.write_text(iso(utcnow()))
            drive.download(it["id"], dest, busy)
            new += 1
    finally:
        busy.unlink(missing_ok=True)
    return new


def watch(c, s, min_dur):
    """Watcher hook for a 'gdrive' source: sync every few hours, then clip new files like a folder."""
    from . import notify, watcher
    root = config.data_dir() / "gdrive" / hashlib.sha1(s["channel_id"].encode()).hexdigest()[:12]
    key = f"gdrive_last:{s['id']}"
    last, retry = db.kv_get(key), db.kv_get(key + ":retry")
    every = config.section("watcher").get("dropbox_every_hours", 6) * 3600
    due = not last or (utcnow() - parse_iso(last)).total_seconds() >= every or not (root / ".complete").exists()
    if due and not (retry and utcnow() < parse_iso(retry)):
        try:
            new = sync(c, s, root)
        except Exception as e:
            minutes = config.section("watcher").get("dropbox_retry_minutes", 2)
            db.kv_set(key + ":retry", iso(utcnow() + dt.timedelta(minutes=minutes)))
            db.kv_set("watcher_soon", db.kv_get(key + ":retry"))
            raise RuntimeError(f"Google Drive download failed, trying again in {minutes} min: {str(e)[:200]}") from None
        (root / ".complete").write_text(iso(utcnow()))
        db.kv_set(key, iso(utcnow()))
        db.kv_set(key + ":retry", None)
        if new:
            notify.send("progress", f"✅ Google Drive for **{c['name']}**: {new} new video(s) downloaded. "
                        "The AI looks for moments next.", campaign_id=c["id"])
    if not root.exists():
        return 0
    return watcher._folder(c, {**s, "channel_id": str(root)}, min_dur)


def selftest_line():
    d = Drive()
    if not d.creds:
        raise NeedsLogin("not connected; run: clipfactory auth-drive main (only needed for private Drive folders)")
    d._get("/about", {"fields": "user(displayName)"})
    return "can read Drive folders shared with you (read-only)"

