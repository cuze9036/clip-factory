"""Stage 3: download only the seconds of each moment, not the whole stream."""
import pathlib

from . import config
from .util import run


def moment_path(campaign_id, moment_id):
    d = config.data_dir() / "clips" / campaign_id
    d.mkdir(parents=True, exist_ok=True)
    return d / f"m{moment_id}_raw.mp4"


def fetch(video, start, end, out):
    out = pathlib.Path(out)
    if video["local_path"]:
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-to", f"{end:.2f}", "-i", video["local_path"],
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", "-b:a", "192k", out])
        return out
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import download_range_func

    h = config.section("download").get("max_height", 1080)
    base = out.with_suffix("")
    opts = {
        "format": f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/b",
        "outtmpl": str(base) + ".%(ext)s",
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "overwrites": True,
    }
    cookies = config.section("download").get("cookies_file")
    if cookies:
        opts["cookiefile"] = str(config.path(cookies))
    with YoutubeDL(opts) as y:
        y.download([video["url"]])
    got = sorted(base.parent.glob(base.name + ".*"), key=lambda p: p.stat().st_mtime)
    if not got:
        raise RuntimeError("yt-dlp produced no file")
    if got[-1] != out:
        got[-1].replace(out)
    even_streams(out)
    return out


def stream_lengths(path):
    """{"video": seconds, "audio": seconds} of the file's streams (missing ones left out)."""
    import json
    import subprocess
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json",
                        str(path)], capture_output=True, text=True)
    out = {}
    for s in json.loads(r.stdout or "{}").get("streams", []):
        try:
            out.setdefault(s["codec_type"], float(s["duration"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def even_streams(path, slack=0.3):
    """A section cut from YouTube sometimes has a picture that ends seconds before the sound (a GTA clip had
    30.7 s of video and 36 s of audio, so its last 5 s were a frozen frame). Cut both to the shorter one."""
    path = pathlib.Path(path)
    n = stream_lengths(path)
    if "video" not in n or "audio" not in n or abs(n["video"] - n["audio"]) <= slack:
        return False
    tmp = path.with_name(path.stem + "_even.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-i", path, "-t", f"{min(n['video'], n['audio']):.3f}", "-c", "copy",
         "-movflags", "+faststart", tmp])
    tmp.replace(path)
    return True
