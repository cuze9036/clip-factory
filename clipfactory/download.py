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
    return out
