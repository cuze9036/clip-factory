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
    if platform in (config.get().get("always_manual") or []):
        return "manual"
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

    from googleapiclient.errors import HttpError

    yt = build("youtube", "v3", credentials=youtube_creds(post["account"]), cache_discovery=False)
    part, body = youtube_body(post, clip, _campaign(post))

    def send(part, body):
        media = MediaFileUpload(clip["final_path"], mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        req = yt.videos().insert(part=part, body=body, media_body=media)
        resp = None
        while resp is None:
            _, resp = req.next_chunk()
        return resp

    try:
        resp = send(part, body)
    except HttpError as e:
        if "paidProductPlacement" not in str(e):
            raise
        # YouTube refused the paid-promotion flag: upload without it (the #Sponsored tag still leads the description)
        db.log(post["campaign_id"], f"YouTube refused the paid-promotion flag, uploaded without it: {str(e)[:200]}", "warn")
        body.pop("paidProductPlacementDetails", None)
        resp = send("snippet,status", body)
    return resp["id"], f"https://www.youtube.com/shorts/{resp['id']}"


def youtube_body(post, clip, campaign):
    """(part, body) for videos.insert. Paid campaigns: #Sponsored first in the description, and YouTube's own
    paid-promotion flag (paidProductPlacementDetails.hasPaidProductPlacement)."""
    from .metadata import strip_tag
    when = parse_iso(post["scheduled_at"])
    status = {"selfDeclaredMadeForKids": False}
    if when > utcnow() + dt.timedelta(minutes=5):
        status.update(privacyStatus="private", publishAt=iso(when))
    else:
        status["privacyStatus"] = "public"
    tag = _disclosure(clip, campaign)
    desc = strip_tag(clip["description"], tag)
    if tag:
        desc = f"{tag}\n\n{desc}"
    tags = [h.lstrip("#") for h in json.loads(clip["hashtags"] or "[]") if not (tag and h.lower() == tag.lower())]
    body = {"snippet": {"title": clip["title"][:100], "description": desc[:4900], "tags": tags,
                        "categoryId": "20", "defaultLanguage": "en", "defaultAudioLanguage": "en"},
            "status": status}
    part = "snippet,status"
    if tag:
        body["paidProductPlacementDetails"] = {"hasPaidProductPlacement": True}
        part += ",paidProductPlacementDetails"
    return part, body


# ---------- Instagram & Facebook (Graph API, resumable upload of the local file) ----------

def _graph(path):
    return f"https://graph.facebook.com/{config.get().get('graph_api_version', 'v21.0')}/{path}"


def _check(r):
    if r.status_code >= 400:
        raise RuntimeError(f"Graph API {r.status_code}: {r.text[:400]}")
    return r.json()


def meta_keys(platform, account="main"):
    """(id, token) for Instagram/Facebook from .env, cleaned of paste slips: Meta IDs are digits only, and Meta
    tokens start with "EAA" (a stray character typed before the paste is dropped)."""
    acc = _account(platform, account)
    raw_id = config.env(acc["user_id_env"] if platform == "instagram" else acc["page_id_env"]) or ""
    token = (config.env(acc["token_env"]) or "").strip().strip('"\'')
    if "EAA" in token and not token.startswith("EAA"):
        token = token[token.index("EAA"):]
    return re.sub(r"\D", "", raw_id), token


def _g(method, url, token, **kw):
    """A Graph API call with the token in a header (never in the URL), so no error message or log line shows it."""
    headers = {"Authorization": f"Bearer {token}", **(kw.pop("headers", None) or {})}
    try:
        r = requests.request(method, url, headers=headers, **kw)
    except requests.RequestException as e:
        raise RuntimeError(f"Meta could not be reached ({type(e).__name__})") from None
    return _check(r)


NAMES = {"youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok", "x": "X"}


def name(platform):
    return NAMES.get(platform, platform.title())


def _campaign(post):
    from . import campaigns
    return campaigns.get(post["campaign_id"]) if post else None


def _disclosure(clip, campaign):
    from .metadata import disclosure_tag
    return disclosure_tag(campaign, json.loads(clip["hashtags"] or "[]"))


def _caption(clip, platform=None, campaign=None):
    """First line: TITLE plus the paid-promotion tag (so it shows before "more"), then the description."""
    from .metadata import strip_tag
    tag = _disclosure(clip, campaign)
    first = f"{clip['title']} {tag}".strip()
    desc = strip_tag(clip["description"], tag)
    if platform == "x":   # X posts allow 280 characters: first line, credit line, then hashtags while they fit
        credit = next((ln for ln in desc.splitlines() if ln.lower().startswith("credit")), "")
        text = first[:280]
        if credit and len(text) + 2 + len(credit) <= 280:
            text += "\n\n" + credit
        tags = [t for t in json.loads(clip["hashtags"] or "[]") if not (tag and t.lower() == tag.lower())]
        line = ""
        for t in tags:
            if len(text) + 2 + len((line + " " + t).strip()) > 280:
                break
            line = (line + " " + t).strip()
        return text + ("\n\n" + line if line else "")
    return f"{first}\n\n{desc}".strip()[:2200]


def instagram_upload(post, clip):
    acc = _account("instagram", post["account"])
    uid, token = meta_keys("instagram", post["account"])
    if not uid or not token:
        raise RuntimeError(f"Set {acc['user_id_env']} and {acc['token_env']} in .env")
    created = _g("POST", _graph(f"{uid}/media"), token, data={
        "media_type": "REELS", "upload_type": "resumable", "caption": _caption(clip, "instagram", _campaign(post)),
        "share_to_feed": "true"}, timeout=60)
    cid = created["id"]
    size = os.path.getsize(clip["final_path"])
    ver = config.get().get("graph_api_version", "v21.0")
    with open(clip["final_path"], "rb") as f:
        _g("POST", f"https://rupload.facebook.com/ig-api-upload/{ver}/{cid}", token, data=f, timeout=600,
           headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)})
    for _ in range(60):
        st = _g("GET", _graph(cid), token, params={"fields": "status_code"}, timeout=30)
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError("Instagram could not process the video")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram processing timed out")
    media = _g("POST", _graph(f"{uid}/media_publish"), token, data={"creation_id": cid}, timeout=60)
    link = _g("GET", _graph(media["id"]), token, params={"fields": "permalink"}, timeout=30).get("permalink")
    return media["id"], link


def facebook_upload(post, clip):
    acc = _account("facebook", post["account"])
    page, token = meta_keys("facebook", post["account"])
    if not page or not token:
        raise RuntimeError(f"Set {acc['page_id_env']} and {acc['token_env']} in .env")
    start = _g("POST", _graph(f"{page}/video_reels"), token, data={"upload_phase": "start"}, timeout=60)
    vid = start["video_id"]
    size = os.path.getsize(clip["final_path"])
    ver = config.get().get("graph_api_version", "v21.0")
    with open(clip["final_path"], "rb") as f:
        _g("POST", f"https://rupload.facebook.com/video-upload/{ver}/{vid}", token, data=f, timeout=600,
           headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)})
    _g("POST", _graph(f"{page}/video_reels"), token, data={
        "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
        "description": _caption(clip, "facebook", _campaign(post))}, timeout=60)
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


def when_text(post):
    """'9:00 PM ET (7:30 AM IST)' for a post's time."""
    from zoneinfo import ZoneInfo
    from .util import ET
    t = parse_iso(post["scheduled_at"])
    et = t.astimezone(ET)
    out = f"{et:%I:%M %p}".lstrip("0") + " ET"
    tz = config.get().get("owner_timezone")
    if tz:
        try:
            own = t.astimezone(ZoneInfo(tz))
            day = "" if own.date() == et.date() else f" {own:%a}"
            out += f" ({own:%I:%M %p}".replace("(0", "(") + f"{day} {own.tzname()})"
        except Exception:
            pass
    return out


def is_manual(post):
    return post.get("mode") == "manual" or post["platform"] in (config.get().get("always_manual") or [])


def _ready_files(post, clip):
    import shutil
    safe = re.sub(r'[<>:"/\\|?*]+', "", clip["title"] or "clip")[:50].strip()
    base = ready_folder(post["platform"]) / f"#{post['id']} {safe}"
    shutil.copyfile(clip["final_path"], f"{base}.mp4")
    caption = _caption(clip, post["platform"], _campaign(post))
    with open(f"{base}.txt", "w", encoding="utf-8") as f:
        f.write(caption)
    return f"#{post['id']} {safe}", caption


def deliver(post, clip):
    """Early: the video and caption go to Telegram (and 'Ready to post') hours before the posting time."""
    name_, caption = _ready_files(post, clip)
    camp = db.one("SELECT name FROM campaigns WHERE id=?", post["campaign_id"])
    from . import telegram
    try:
        telegram.hand_off(post, clip, caption, camp["name"], when_text(post))
    except Exception as e:
        db.log(post["campaign_id"], f"Telegram hand-off failed for post {post['id']}: {e}", "warn")
    db.x("UPDATE posts SET delivered_at=? WHERE id=?", iso(utcnow()), post["id"])
    return name_


def remind(post, clip):
    """Shortly before the posting time: a short Discord reminder (the video is already on Telegram)."""
    from . import telegram
    within = config.section("manual_posting").get("remind_minutes_before", 30)
    camp = db.one("SELECT name FROM campaigns WHERE id=?", post["campaign_id"])
    where = "on Telegram" if telegram.creds() else "in Clipping Tools\\Ready to post\\" + post["platform"]
    db.x("UPDATE posts SET status='manual', reminded_at=?, error=NULL WHERE id=?", iso(utcnow()), post["id"])
    notify.send("manual", f"⏰ **Time to post on {name(post['platform'])}** at {when_text(post)} · "
                f"post {post['id']} · {camp['name']}\n"
                f"The video and caption are {where} (sent earlier). Post it within {within} minutes, then "
                f"`!posted {post['id']} <link to your post>`.",
                campaign_id=post["campaign_id"], clip_id=post["clip_id"], post_id=post["id"],
                buttons=[["Skip this one", f"skip:{post['id']}", "grey"]])


def hand_off(post, clip):
    """Both steps at once (a post that is already due)."""
    if not post.get("delivered_at"):
        deliver(post, clip)
    remind(post, clip)


def run_manual():
    """Posts you make by hand: deliver early to Telegram, remind on Discord shortly before the time."""
    cfg = config.section("manual_posting")
    now = utcnow()
    horizon = iso(now + dt.timedelta(hours=cfg.get("deliver_hours_before", 24)))
    remind_at = dt.timedelta(minutes=cfg.get("remind_minutes_before", 30))
    # A platform that can now post by itself (keys added, or YouTube no longer hand-posted): its queued posts
    # that weren't announced yet switch to automatic.
    for post in db.q("SELECT * FROM posts WHERE status='queued' AND mode='manual' AND reminded_at IS NULL"):
        if db.kv_get(f"handed:{post['id']}"):
            continue   # auto-fix gave it to you because posting by itself failed: it stays with you
        if post["platform"] not in (config.get().get("always_manual") or []) and \
                post["platform"] in UPLOADERS and _has_keys(post["platform"], post["account"]):
            db.x("UPDATE posts SET mode='auto' WHERE id=?", post["id"])
            if post.get("delivered_at"):
                db.log(post["campaign_id"], f"Post {post['id']} ({post['platform']}) now goes out by itself; "
                       "ignore the copy on Telegram")
    for post in db.q("SELECT * FROM posts WHERE status='queued' AND scheduled_at <= ? ORDER BY scheduled_at", horizon):
        if not is_manual(post):
            continue
        clip = db.one("SELECT * FROM clips WHERE id=?", post["clip_id"])
        try:
            if not post.get("delivered_at"):
                deliver(post, clip)
                post = db.one("SELECT * FROM posts WHERE id=?", post["id"])
            if parse_iso(post["scheduled_at"]) - remind_at <= now:
                remind(post, clip)
        except Exception as e:
            db.log(post["campaign_id"], f"Couldn't prepare post {post['id']}: {e}", "error")


def mark_posted(post_id, url):
    db.x("UPDATE posts SET status='posted', url=?, posted_at=?, error=NULL WHERE id=?", url, iso(utcnow()), post_id)
    submit_reminder(post_id)
    for r in db.q("""SELECT c.id FROM clips c WHERE c.status='scheduled' AND NOT EXISTS
                     (SELECT 1 FROM posts p WHERE p.clip_id=c.id AND p.status NOT IN ('posted','failed'))"""):
        db.x("UPDATE clips SET status='posted' WHERE id=?", r["id"])


def night_post(post):
    """A campaign clip that would go live while the owner sleeps (late upload, old schedule) moves to the next
    time they're awake, so the Whop submit window doesn't run out. True when it was moved."""
    from . import campaigns, schedule
    c = campaigns.get(post["campaign_id"])
    live = max(parse_iso(post["scheduled_at"]), utcnow())
    if not c or not schedule.needs_submit(c) or schedule.awake(live, config.section("submit").get("within_minutes", 30)):
        return False
    when = schedule.next_slot(c, post["platform"], post["account"])
    db.x("UPDATE posts SET scheduled_at=? WHERE id=?", iso(when), post["id"])
    db.log(post["campaign_id"], f"{NAMES.get(post['platform'], post['platform'])} post {post['id']} moved to "
           f"{when_text({'scheduled_at': iso(when)})}: you'd be asleep at its time and couldn't submit it")
    return True


def run_due():
    run_manual()
    submit_nudges()
    done = 0
    for post in due_posts():
        clip = db.one("SELECT * FROM clips WHERE id=?", post["clip_id"])
        if is_manual(post):
            continue   # handled by run_manual()
        if post["platform"] == "youtube":
            key, n = youtube_uploads_today()
            if n >= config.section("schedule").get("youtube_uploads_per_day", 6):
                continue
        if night_post(post):
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


def _submit_row(post_id):
    p = db.one("""SELECT p.*, c.name campaign, c.url campaign_url, c.origin FROM posts p
                  JOIN campaigns c ON c.id=p.campaign_id WHERE p.id=?""", post_id)
    if not p or p["submitted"] or not p["url"] or (p["origin"] == "yaml" and not p["campaign_url"]):
        return None   # own channel: nothing to submit
    return p


NAMES = {"youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok", "x": "X"}


def reel_number(p):
    """1, 2, 3 ... per campaign and platform, in posting order."""
    return db.one("""SELECT COUNT(*) n FROM posts WHERE campaign_id=? AND platform=? AND status='posted'
                     AND url IS NOT NULL AND (posted_at < ? OR (posted_at = ? AND id <= ?))""",
                  p["campaign_id"], p["platform"], p["posted_at"], p["posted_at"], p["id"])["n"] or 1


def submit_reminder(post_id):
    """Right after a clip goes live: one short message (campaign, reel number, deadline, Submitted button) and,
    on its own, the link, so a long-press copies only the link."""
    p = _submit_row(post_id)
    if not p:
        return
    minutes = config.section("submit").get("within_minutes", 30)
    deadline = when_text({"scheduled_at": iso(utcnow() + dt.timedelta(minutes=minutes))})
    kind = "Reel" if p["platform"] in ("instagram", "facebook") else "Short" if p["platform"] == "youtube" else "Post"
    head = (f"📌 **Submit now: {p['campaign']}**\n"
            f"{NAMES.get(p['platform'], p['platform'].title())} {kind} {reel_number(p)} · submit by {deadline}\n"
            f"Copy the link below → Whop: {('<' + p['campaign_url'] + '>') if p['campaign_url'] else 'the campaign page'}"
            "\nSubmitted it? Tap ✅ Done.")
    notify.send("submit", head, campaign_id=p["campaign_id"], post_id=post_id,
                buttons=[["✅ Done", f"sub:{post_id}", "green"]])
    notify.send("submit", p["url"], campaign_id=p["campaign_id"], post_id=post_id)
    db.kv_set(f"submit_ping:{post_id}", iso(utcnow()))


def submit_nudges():
    """Not marked Submitted after a while: one more short ping, with the link on its own again."""
    cfg = config.section("submit")
    after = dt.timedelta(minutes=cfg.get("second_reminder_minutes", 20))
    for k in db.q("SELECT k, v FROM kv WHERE k LIKE 'submit_ping:%'"):
        post_id = int(k["k"].split(":")[1])
        try:
            sent = parse_iso(json.loads(k["v"]))
        except (TypeError, ValueError):
            sent = None
        if not sent or utcnow() - sent < after:
            continue
        db.x("DELETE FROM kv WHERE k=?", k["k"])   # one extra ping per post
        p = _submit_row(post_id)
        if not p:
            continue
        left = cfg.get("within_minutes", 30) - cfg.get("second_reminder_minutes", 20)
        notify.send("submit", f"⏰ **{p['campaign']}**: about {left} min left to submit this one "
                    f"({NAMES.get(p['platform'], p['platform'].title())} {reel_number(p)}).", campaign_id=p["campaign_id"], post_id=post_id,
                    buttons=[["✅ Done", f"sub:{post_id}", "green"]])
        notify.send("submit", p["url"], campaign_id=p["campaign_id"], post_id=post_id)
