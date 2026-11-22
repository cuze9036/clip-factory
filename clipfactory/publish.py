"""Stage 7: upload to YouTube Shorts, Instagram Reels and Facebook Reels."""
import datetime as dt
import json
import os
import re
import time

import requests

from . import config, db, notify
from .util import iso, parse_iso, utcnow

YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
             "https://www.googleapis.com/auth/youtube.readonly",
             "https://www.googleapis.com/auth/yt-analytics.readonly"]


def _account(platform, name):
    acc = ((config.get().get("accounts") or {}).get(platform) or {}).get(name)
    if not acc:
        raise RuntimeError(f"No {platform} account named {name!r} in config.yaml")
    return acc


def mode(platform, account):
    """'auto' when the tool can post there itself, 'manual' when it hands the clip to you, else None."""
    if platform in UPLOADERS and _has_keys(platform, account):
        return "auto"
    if platform in (config.get().get("manual_platforms") or []):
        return "manual"
    return None


def platform_ready(platform, account):
    return mode(platform, account) is not None


def _has_keys(platform, account):
    """True when this account's login or keys are set, so posting there can work."""
    try:
        acc = _account(platform, account)
    except RuntimeError:
        return False
    if platform == "youtube":
        return config.path(acc["token_file"]).exists()
    if platform == "instagram":
        return bool(config.env(acc["user_id_env"]) and config.env(acc["token_env"]))
    if platform == "facebook":
        return bool(config.env(acc["page_id_env"]) and config.env(acc["token_env"]))
    return False


# ---------- YouTube ----------

def youtube_auth(account="main"):
    from google_auth_oauthlib.flow import InstalledAppFlow
    acc = _account("youtube", account)
    flow = InstalledAppFlow.from_client_secrets_file(str(config.path(acc["client_secrets"])), YT_SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    token = config.path(acc["token_file"])
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    os.chmod(token, 0o600)
    print(f"Saved YouTube token to {token}")


def youtube_forget(account="main"):
    """Revoke the YouTube login at Google, delete the saved token and the stored YouTube statistics."""
    token = config.path(_account("youtube", account)["token_file"])
    revoked = False
    if token.exists():
        data = json.loads(token.read_text() or "{}")
        t = data.get("refresh_token") or data.get("token")
        if t:
            r = requests.post("https://oauth2.googleapis.com/revoke", data={"token": t}, timeout=30)
            revoked = r.status_code == 200
        token.unlink()
    n = db.conn().execute("UPDATE posts SET views=0, likes=0, us_share=NULL, stats_at=NULL "
                          "WHERE platform='youtube' AND account=?", (account,)).rowcount
    return revoked, n


def youtube_creds(account):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    token = config.path(_account("youtube", account)["token_file"])
    if not token.exists():
        raise RuntimeError(f"YouTube account {account!r} not connected. Run: clipfactory auth-youtube {account}")
    from google.auth.exceptions import RefreshError
    creds = Credentials.from_authorized_user_file(str(token), YT_SCOPES)
    if not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # Apps in Google's "Testing" mode get logged out every 7 days. Tell the owner once a day.
            key = "yt_relogin_alert_" + utcnow().date().isoformat()
            if not db.kv_get(key):
                db.kv_set(key, True)
                notify.send("alert", f"YouTube login for '{account}' expired, so uploads are paused.\n"
                            f"On the laptop, open PowerShell in Clipping Tools and run:\n"
                            f"`.\\.venv\\Scripts\\clipfactory.exe auth-youtube {account}`\n"
                            "Then restart Clip Factory. Waiting clips post after that.")
            raise RuntimeError(f"YouTube login expired. Run: clipfactory auth-youtube {account}")
        token.write_text(creds.to_json())
    return creds


def youtube_upload(post, clip):
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    yt = build("youtube", "v3", credentials=youtube_creds(post["account"]), cache_discovery=False)
    when = parse_iso(post["scheduled_at"])
    status = {"selfDeclaredMadeForKids": False}
    if when > utcnow() + dt.timedelta(minutes=5):
        status.update(privacyStatus="private", publishAt=iso(when))
    else:
        status["privacyStatus"] = "public"
    tags = [h.lstrip("#") for h in json.loads(clip["hashtags"] or "[]")]
    body = {"snippet": {"title": clip["title"][:100], "description": clip["description"][:4900], "tags": tags,
                        "categoryId": "20", "defaultLanguage": "en", "defaultAudioLanguage": "en"},
            "status": status}
    media = MediaFileUpload(clip["final_path"], mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"], f"https://www.youtube.com/shorts/{resp['id']}"


# ---------- Instagram & Facebook (Graph API, resumable upload of the local file) ----------

def _graph(path):
    return f"https://graph.facebook.com/{config.get().get('graph_api_version', 'v21.0')}/{path}"


def _check(r):
    if r.status_code >= 400:
        raise RuntimeError(f"Graph API {r.status_code}: {r.text[:400]}")
    return r.json()


NAMES = {"youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok", "x": "X"}


def name(platform):
    return NAMES.get(platform, platform.title())


def _caption(clip, platform=None):
    if platform == "x":   # X posts allow 280 characters: title, credit line and hashtags only
        tags = " ".join(json.loads(clip["hashtags"] or "[]"))
        credit = next((ln for ln in (clip["description"] or "").splitlines() if ln.lower().startswith("credit")), "")
        text = "\n\n".join(p for p in (clip["title"], credit, tags) if p)
        return text if len(text) <= 280 else text[:277].rsplit(" ", 1)[0] + "..."
    return f"{clip['title']}\n\n{clip['description']}"[:2200]


def instagram_upload(post, clip):
    acc = _account("instagram", post["account"])
    uid, token = config.env(acc["user_id_env"]), config.env(acc["token_env"])
    if not uid or not token:
        raise RuntimeError(f"Set {acc['user_id_env']} and {acc['token_env']} in .env")
    created = _check(requests.post(_graph(f"{uid}/media"), data={
        "media_type": "REELS", "upload_type": "resumable", "caption": _caption(clip), "share_to_feed": "true",
        "access_token": token}, timeout=60))
    cid = created["id"]
    size = os.path.getsize(clip["final_path"])
    ver = config.get().get("graph_api_version", "v21.0")
    with open(clip["final_path"], "rb") as f:
        _check(requests.post(f"https://rupload.facebook.com/ig-api-upload/{ver}/{cid}", data=f, timeout=600,
                             headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)}))
    for _ in range(60):
        st = _check(requests.get(_graph(cid), params={"fields": "status_code", "access_token": token}, timeout=30))
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError("Instagram could not process the video")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram processing timed out")
    media = _check(requests.post(_graph(f"{uid}/media_publish"),
                                 data={"creation_id": cid, "access_token": token}, timeout=60))
    link = _check(requests.get(_graph(media["id"]), params={"fields": "permalink", "access_token": token},
                               timeout=30)).get("permalink")
    return media["id"], link


def facebook_upload(post, clip):
    acc = _account("facebook", post["account"])
    page, token = config.env(acc["page_id_env"]), config.env(acc["token_env"])
    if not page or not token:
        raise RuntimeError(f"Set {acc['page_id_env']} and {acc['token_env']} in .env")
    start = _check(requests.post(_graph(f"{page}/video_reels"),
                                 data={"upload_phase": "start", "access_token": token}, timeout=60))
    vid = start["video_id"]
    size = os.path.getsize(clip["final_path"])
    ver = config.get().get("graph_api_version", "v21.0")
    with open(clip["final_path"], "rb") as f:
        _check(requests.post(f"https://rupload.facebook.com/video-upload/{ver}/{vid}", data=f, timeout=600,
                             headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)}))
    _check(requests.post(_graph(f"{page}/video_reels"), data={
        "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED", "description": _caption(clip),
        "access_token": token}, timeout=60))
    return vid, f"https://www.facebook.com/reel/{vid}"


UPLOADERS = {"youtube": youtube_upload, "instagram": instagram_upload, "facebook": facebook_upload}


def due_posts():
    """YouTube uploads right away with a publishAt time; Instagram and Facebook post at their slot."""
    now = iso(utcnow())
    return db.q("""SELECT * FROM posts WHERE status='queued' AND (platform='youtube' OR scheduled_at <= ?)
                   ORDER BY scheduled_at""", now)


def youtube_uploads_today():
    key = "yt_uploads_" + utcnow().astimezone(dt.timezone(dt.timedelta(hours=-8))).date().isoformat()
    return key, db.kv_get(key, 0)


def ready_folder(platform):
    d = config.path("Ready to post") / platform
    d.mkdir(parents=True, exist_ok=True)
    return d


def hand_off(post, clip):
    """Manual platforms: put the clip and its caption in 'Ready to post' and tell you on Discord."""
    import shutil
    safe = re.sub(r'[<>:"/\\|?*]+', "", clip["title"] or "clip")[:50].strip()
    base = ready_folder(post["platform"]) / f"#{post['id']} {safe}"
    shutil.copyfile(clip["final_path"], f"{base}.mp4")
    caption = _caption(clip, post["platform"])
    with open(f"{base}.txt", "w", encoding="utf-8") as f:
        f.write(caption)
    db.x("UPDATE posts SET status='manual', error=NULL WHERE id=?", post["id"])
    camp = db.one("SELECT name, url FROM campaigns WHERE id=?", post["campaign_id"])
    from . import telegram
    on_phone = "The video and caption are on Telegram. " if telegram.creds() else ""
    notify.send("manual", f"Time to post on {name(post['platform'])} (post {post['id']}) · {camp['name']}\n"
                f"{on_phone}Video also in: Clipping Tools\\Ready to post\\{post['platform']}\\#{post['id']} {safe}.mp4\n"
                f"Caption:\n```\n{caption[:1200]}\n```\n"
                f"After posting, type `!posted {post['id']} <link to your post>`",
                campaign_id=post["campaign_id"], clip_id=post["clip_id"], post_id=post["id"],
                file_path=clip["final_path"], buttons=[["Skip this one", f"skip:{post['id']}", "grey"]])
    try:
        telegram.hand_off(post, clip, caption, camp["name"])
    except Exception as e:
        db.log(post["campaign_id"], f"Telegram hand-off failed for post {post['id']}: {e}", "warn")


def mark_posted(post_id, url):
    db.x("UPDATE posts SET status='posted', url=?, posted_at=?, error=NULL WHERE id=?", url, iso(utcnow()), post_id)
    submit_reminder(post_id)
    for r in db.q("""SELECT c.id FROM clips c WHERE c.status='scheduled' AND NOT EXISTS
                     (SELECT 1 FROM posts p WHERE p.clip_id=c.id AND p.status NOT IN ('posted','failed'))"""):
        db.x("UPDATE clips SET status='posted' WHERE id=?", r["id"])


def run_due():
    done = 0
    for post in due_posts():
        clip = db.one("SELECT * FROM clips WHERE id=?", post["clip_id"])
        if post.get("mode") == "manual":
            if parse_iso(post["scheduled_at"]) <= utcnow():
                try:
                    hand_off(post, clip)
                except Exception as e:
                    db.log(post["campaign_id"], f"Couldn't prepare post {post['id']}: {e}", "error")
            continue
        if post["platform"] == "youtube":
            key, n = youtube_uploads_today()
            if n >= config.section("schedule").get("youtube_uploads_per_day", 6):
                continue
        try:
            ext_id, url = UPLOADERS[post["platform"]](post, clip)
            uploaded_early = post["platform"] == "youtube" and parse_iso(post["scheduled_at"]) > utcnow()
            db.x("UPDATE posts SET status=?, external_id=?, url=?, posted_at=?, error=NULL WHERE id=?",
                 "uploaded" if uploaded_early else "posted", ext_id, url, post["scheduled_at"], post["id"])
            if post["platform"] == "youtube":
                db.kv_set(key, n + 1)
            if not uploaded_early:
                submit_reminder(post["id"])
            db.log(post["campaign_id"], f"{post['platform'].title()} {'scheduled' if uploaded_early else 'posted'}: {url}")
            done += 1
        except Exception as e:
            if "auth-youtube" in str(e):
                # Not the clip's fault: keep it queued so it posts once you log in again.
                db.x("UPDATE posts SET error=? WHERE id=?", str(e)[:500], post["id"])
                continue
            db.x("UPDATE posts SET status='failed', error=? WHERE id=?", str(e)[:500], post["id"])
            db.log(post["campaign_id"], f"{post['platform']} upload failed: {e}", "error")
    # Scheduled YouTube uploads count as posted once their time passes.
    for p in db.q("SELECT id FROM posts WHERE status='uploaded' AND scheduled_at <= ?", iso(utcnow())):
        db.x("UPDATE posts SET status='posted' WHERE id=?", p["id"])
        submit_reminder(p["id"])
    for r in db.q("""SELECT c.id FROM clips c WHERE c.status='scheduled' AND NOT EXISTS
                     (SELECT 1 FROM posts p WHERE p.clip_id=c.id AND p.status NOT IN ('posted','failed'))"""):
        db.x("UPDATE clips SET status='posted' WHERE id=?", r["id"])
    return done


def submit_reminder(post_id):
    p = db.one("""SELECT p.*, c.name campaign, c.url campaign_url, c.platform site, k.title FROM posts p
                  JOIN campaigns c ON c.id=p.campaign_id JOIN clips k ON k.id=p.clip_id WHERE p.id=?""", post_id)
    if not p or not p["campaign_url"] or p["submitted"]:
        return
    notify.send("submit", f"Live now on {p['platform'].title()}: {p['url']}\n"
                f"Submit it on {p['site'] or 'the campaign page'} within 30 minutes: {p['campaign_url']}\n"
                f"{p['campaign']} · {p['title']}", campaign_id=p["campaign_id"], post_id=post_id,
                buttons=[["Submitted", f"sub:{post_id}", "green"]])
