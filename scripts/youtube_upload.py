"""Clip Factory: complete YouTube upload script.

This is the same upload flow Clip Factory uses (clipfactory/publish.py), as one file you can run on its own.

What it does:
  1. Signs the owner in with Google OAuth 2.0 (installed-app flow, browser consent screen).
     Scopes: youtube.upload, youtube.readonly, yt-analytics.readonly.
  2. Saves the OAuth token on this computer only (secrets/youtube_main.json).
  3. Uploads one vertical MP4 to the owner's own channel with videos.insert (resumable upload),
     with a title, description, tags, and either "publish now" or a scheduled publish time.
  4. Reads the new video back with videos.list to confirm it is processing on YouTube.

Run:
  pip install google-api-python-client google-auth-oauthlib
  python scripts/youtube_upload.py clip.mp4 --title "WAIT FOR IT" --description "Credit: @creator"
  python scripts/youtube_upload.py clip.mp4 --title "..." --publish-at 2026-12-01T14:00:00Z

Revoke access at any time: https://myaccount.google.com/permissions
or run:  clipfactory forget-youtube main   (revokes the token at Google and deletes it from this computer)
"""
import argparse
import datetime as dt
import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/yt-analytics.readonly"]
ROOT = Path(__file__).resolve().parent.parent
CLIENT_SECRETS = ROOT / "secrets" / "client_secret.json"
TOKEN = ROOT / "secrets" / "youtube_main.json"


def credentials():
    """Load the saved token, refresh it, or run the Google consent screen once."""
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if creds and not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    TOKEN.write_text(creds.to_json())
    os.chmod(TOKEN, 0o600)
    return creds


def upload(path, title, description, tags, publish_at=None):
    yt = build("youtube", "v3", credentials=credentials(), cache_discovery=False)
    status = {"selfDeclaredMadeForKids": False}
    if publish_at:
        status.update(privacyStatus="private", publishAt=publish_at)   # YouTube makes it public at that time
    else:
        status["privacyStatus"] = "public"
    body = {"snippet": {"title": title[:100], "description": description[:4900], "tags": tags,
                        "categoryId": "20", "defaultLanguage": "en", "defaultAudioLanguage": "en"},
            "status": status}
    media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        progress, response = request.next_chunk()
        if progress:
            print(f"Uploaded {int(progress.progress() * 100)}%")
    video_id = response["id"]
    check = yt.videos().list(part="status,snippet", id=video_id).execute()["items"][0]
    print(f"Done: https://www.youtube.com/shorts/{video_id}")
    print(f"Title: {check['snippet']['title']} | privacy: {check['status']['privacyStatus']} "
          f"| upload status: {check['status']['uploadStatus']}")
    return video_id


def main(argv=None):
    p = argparse.ArgumentParser(description="Upload one clip to the owner's YouTube channel")
    p.add_argument("video", type=Path)
    p.add_argument("--title", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--tags", default="", help="comma separated, e.g. GTA6,gaming")
    p.add_argument("--publish-at", help="UTC time like 2026-12-01T14:00:00Z; default publishes now")
    a = p.parse_args(argv)
    if not a.video.exists():
        sys.exit(f"File not found: {a.video}")
    if a.publish_at:
        dt.datetime.fromisoformat(a.publish_at.replace("Z", "+00:00"))   # fail early on a bad time
    upload(a.video, a.title, a.description, [t.strip() for t in a.tags.split(",") if t.strip()], a.publish_at)


if __name__ == "__main__":
    main()
