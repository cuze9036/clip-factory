"""Read-only YouTube Data API calls with an API key."""
import requests

from . import config

BASE = "https://www.googleapis.com/youtube/v3/"


def _get(resource, **params):
    key = config.env("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY is not set in .env")
    r = requests.get(BASE + resource, params={**params, "key": key}, timeout=30)
    if r.status_code >= 400:   # not raise_for_status: its message would show the key
        raise RuntimeError(f"YouTube API {resource} returned HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def resolve_channel(ident):
    """'@handle' or 'UC...' -> 'UC...'"""
    if not ident.startswith("@"):
        return ident
    items = _get("channels", part="id", forHandle=ident).get("items") or []
    if not items:
        raise RuntimeError(f"No YouTube channel for {ident}")
    return items[0]["id"]


def recent_uploads(channel_id, n=10):
    playlist = "UU" + channel_id[2:]
    data = _get("playlistItems", part="contentDetails", playlistId=playlist, maxResults=n)
    return [i["contentDetails"]["videoId"] for i in data.get("items", [])]


def videos(ids):
    out = []
    for i in range(0, len(ids), 50):
        data = _get("videos", part="snippet,contentDetails,liveStreamingDetails,statistics,status",
                    id=",".join(ids[i:i + 50]))
        out.extend(data.get("items", []))
    return out


def stats(ids):
    return {v["id"]: v.get("statistics", {}) for v in videos(ids)}
