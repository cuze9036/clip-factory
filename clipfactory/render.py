"""Stage 4: turn a raw moment into a finished 1080x1920 short."""
import pathlib
import random
import re

from . import config
from .util import ffprobe, run


def _ass_time(t):
    t = max(0.0, t)
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(text):
    return re.sub(r"[{}\\]", "", text).replace("\n", " ").strip()


def group_words(words, size):
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= size or w["s"] - cur[-1]["e"] > 0.6):
            groups.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?]$", w["w"]):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def build_ass(words, hook, duration, cfg):
    W, H = cfg["width"], cfg["height"]
    font, size = cfg.get("font", "Arial"), cfg.get("caption_size", 84)
    hl = cfg.get("highlight_color", "&H0000FFFF&")
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,7,3,2,120,170,620,1",
        f"Style: Hook,{font},{int(size * 0.85)},&H00FFFFFF,&H00FFFFFF,&H00101010,&H00101010,-1,0,0,0,100,100,0,0,3,18,0,8,90,90,280,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if hook:
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(min(cfg.get('hook_seconds', 2.5), duration))},"
                     f"Hook,,0,0,0,,{_clean(hook).upper()}")
    groups = group_words(words, cfg.get("words_per_group", 3))
    for gi, g in enumerate(groups):
        nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else duration
        g_end = min(g[-1]["e"] + 0.25, nxt)
        for wi, w in enumerate(g):
            start = w["s"]
            end = g[wi + 1]["s"] if wi + 1 < len(g) else g_end
            if end <= start:
                continue
            text = " ".join(
                (f"{{\\c{hl}}}{_clean(x['w']).upper()}{{\\c&H00FFFFFF&}}" if j == wi else _clean(x["w"]).upper())
                for j, x in enumerate(g))
            lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def _escape_filter_path(p):
    return str(p).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def video_filter(layout, facecam, cfg, src):
    W, H = cfg["width"], cfg["height"]
    if layout == "facecam_split" and facecam:
        fx, fy, fw, fh = facecam
        face_h = int(H / 3) // 2 * 2
        game_h = H - face_h
        return (f"[0:v]crop=iw*{fw}:ih*{fh}:iw*{fx}:ih*{fy},scale={W}:{face_h}:force_original_aspect_ratio=increase,"
                f"crop={W}:{face_h}[face];"
                f"[0:v]scale=-2:{game_h},crop={W}:{game_h}[game];[face][game]vstack[v0]")
    if layout == "crop":
        return f"[0:v]scale=-2:{H},crop={W}:{H}[v0]"
    fg_h = cfg.get("blur_fg_height", 760)
    return (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"boxblur=20:2,eq=brightness=-0.08[bg];"
            f"[b]scale=-2:{fg_h},crop='min(iw,{W})':{fg_h}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]")


def render(raw, out, words, hook, layout="blur", facecam=None):
    cfg = config.section("render")
    raw, out = pathlib.Path(raw), pathlib.Path(out)
    src = ffprobe(raw)
    dur = src["duration"]
    ass = out.with_suffix(".ass")
    ass.write_text(build_ass(words, hook, dur, cfg), encoding="utf-8")

    vf = video_filter(layout, facecam, cfg, src)
    fps = f",fps={cfg.get('max_fps', 60)}" if src["fps"] > cfg.get("max_fps", 60) + 0.5 else ""
    fonts = config.path(cfg.get("fonts_dir", "assets/fonts"))
    fontsdir = f":fontsdir='{_escape_filter_path(fonts)}'" if fonts.is_dir() else ""
    vf += f";[v0]ass='{_escape_filter_path(ass)}'{fontsdir}{fps},format=yuv420p[v]"

    cmd = ["ffmpeg", "-y", "-v", "error", "-i", raw]
    music = _pick_music(cfg)
    lufs = cfg.get("loudness_lufs", -14)
    if not src["has_audio"]:
        cmd += ["-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
        af = "[1:a]anull[a]"
    elif music:
        cmd += ["-stream_loop", "-1", "-i", music]
        af = (f"[0:a]loudnorm=I={lufs}:TP=-1.5:LRA=11[voice];[1:a]volume={cfg.get('music_gain_db', -20)}dB[m];"
              f"[voice][m]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        af = f"[0:a]loudnorm=I={lufs}:TP=-1.5:LRA=11[a]"
    cmd += ["-filter_complex", f"{vf};{af}", "-map", "[v]", "-map", "[a]", "-t", f"{dur:.2f}",
            "-c:v", "libx264", "-preset", cfg.get("preset", "medium"), "-crf", str(cfg.get("crf", 20)),
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", out]
    run(cmd)
    return {"path": str(out), "duration": dur}


def _pick_music(cfg):
    d = cfg.get("music_dir")
    if not d:
        return None
    p = config.path(d)
    tracks = [t for t in p.glob("*") if t.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg")] if p.is_dir() else []
    return str(random.choice(tracks)) if tracks else None
