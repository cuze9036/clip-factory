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


def _keyword(words):
    """The word to colour in the title: the longest one with 4+ letters."""
    cands = [w for w in words if len(re.sub(r"[^A-Za-z]", "", w)) >= 4]
    return max(cands, key=len) if cands else None


def build_framed_ass(words, hook, duration, cfg, geo, credit):
    W, H = cfg["width"], cfg["height"]
    font = cfg.get("font", "Arial")
    top, vh = geo["top"], geo["vh"]
    light = cfg.get("framed_background", "white") == "white"
    title_col, title_out = ("&H00000000", 0) if light else ("&H00FFFFFF", 6)
    key_col = "&H00303BFF&" if light else "&H0000D4FF&"      # red on white, #FFD400 on dark
    credit_col = "&H00555555" if light else "&H4CFFFFFF"
    hl = cfg.get("highlight_color", "&H0000D4FF&")
    below = top + vh
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Title,{font},{cfg.get('title_size', 72)},{title_col},{title_col},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{title_out},0,2,60,60,{H - top + 40},1",
        f"Style: Cap,{font},{cfg.get('caption_size', 84)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,8,2,8,80,160,{below + 110},1",
        f"Style: Credit,{font},34,{credit_col},{credit_col},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,8,60,60,{below + 40},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if hook:
        words_t = _clean(hook).upper().split()
        key = _keyword(words_t)
        title = " ".join(f"{{\\c{key_col}}}{w}{{\\c{title_col}&}}" if w == key else w for w in words_t)
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(duration)},Title,,0,0,0,,{title}")
    if credit:
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(duration)},Credit,,0,0,0,,{_clean(credit)}")
    groups = group_words(words, cfg.get("framed_words_per_group", 2))
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


def framed_geometry(src, cfg):
    """Full-width video placed near the top third (taller sources are capped so the text still fits)."""
    W = cfg["width"]
    sw, sh = max(1, src["width"]), max(1, src["height"])
    vw, vh = W, int(round(W * sh / sw / 2)) * 2
    cap = cfg.get("framed_max_video_height", 1000)
    if vh > cap:
        vh, vw = cap, int(round(cap * sw / sh / 2)) * 2
    return {"vw": vw, "vh": vh, "top": cfg.get("framed_video_top", 576)}


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
    if layout == "framed":
        g = framed_geometry(src, cfg)
        bg = cfg.get("framed_background", "white")
        fps = min(60, max(24, round(src.get("fps") or 30)))
        if bg == "blur":
            back = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                    f"boxblur=40:2,drawbox=x=0:y=0:w=iw:h=ih:color=black@0.45:t=fill[bg];[b]")
            return back + f"scale={g['vw']}:{g['vh']}[fg];[bg][fg]overlay=(W-w)/2:{g['top']}[v0]"
        colour = {"white": "white", "black": "0x0A0A0A"}.get(bg, bg)
        return (f"color=c={colour}:s={W}x{H}:r={fps}[bg];[0:v]scale={g['vw']}:{g['vh']}[fg];"
                f"[bg][fg]overlay=(W-w)/2:{g['top']}:shortest=1[v0]")
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


def render(raw, out, words, hook, layout="blur", facecam=None, credit="", style=None, accent=None, plan=None):
    """style: one of styles.STYLE_NAMES (the researched viral looks, with effects from an edit plan);
    without it the older layouts are used. Returns {path, duration, plan}."""
    from . import styles
    cfg = config.section("render")
    raw, out = pathlib.Path(raw), pathlib.Path(out)
    if style in styles.STYLE_NAMES:
        return _render_styled(raw, out, words, hook, style, facecam, credit, accent, plan, cfg)
    src = ffprobe(raw)
    dur = src["duration"]
    ass = out.with_suffix(".ass")
    fonts = config.path(cfg.get("fonts_dir", "assets/fonts"))
    if layout == "framed":
        ass.write_text(build_framed_ass(words, hook, dur, cfg, framed_geometry(src, cfg), credit), encoding="utf-8")
    else:
        ass.write_text(build_ass(words, hook, dur, cfg), encoding="utf-8")
    vf = video_filter(layout, facecam, cfg, src)
    fps = f",fps={cfg.get('max_fps', 60)}" if src["fps"] > cfg.get("max_fps", 60) + 0.5 else ""
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
    cmd += ["-filter_complex", f"{vf};{af}", "-map", "[v]", "-map", "[a]", "-t", f"{dur:.2f}"]
    _encode(cmd, out, cfg)
    return {"path": str(out), "duration": dur, "plan": None}


def _render_styled(raw, out, words, hook, style, facecam, credit, accent, plan, cfg):
    from . import effects, styles
    fonts = styles.ensure_fonts(config.path(cfg.get("fonts_dir", "assets/fonts")))
    src = ffprobe(raw)
    dur = src["duration"]
    eff = config.section("effects")
    on = eff.get("enabled", True)
    if plan is None:
        plan = effects.auto_plan(words, dur, hook, style) if on else {}
    plan = effects.clean(plan, dur)
    used = dict(plan)
    base = raw
    if plan["cuts"]:   # jump cuts first, then everything on the new timeline
        spans = effects.keep_spans(plan["cuts"], dur)
        base = out.with_name(out.stem + "_cut.mp4")
        effects.cut_video(raw, spans, base)
        words, plan = effects.apply_cuts(words, plan, spans)
        src = ffprobe(base)
        dur = src["duration"]
    faces = None
    if on and eff.get("face_tracking", True) and style in ("bold_crop", "word_pop"):
        faces = effects.face_track(base)
    ass = out.with_suffix(".ass")
    ass.write_text(styles.build_ass(style, words, hook, dur, credit, accent or "#FFD400", plan), encoding="utf-8")
    vf = styles.video_filter(style, src, facecam, dur, plan, faces)

    inputs, n, chain = ["-i", str(base)], 1, "[v0]"
    for k, e in enumerate(plan.get("emojis", [])):
        png = effects.emoji_png(e["emoji"])
        if not png:
            continue
        inputs += ["-loop", "1", "-i", str(png)]
        y = styles.LOOK[style]["cap_y"] - 240
        vf += (f";[{n}:v]scale=150:150,format=rgba[e{k}];{chain}[e{k}]overlay=x=465:y={y}:shortest=1:"
               f"enable='between(t,{e['t']:.2f},{e['t'] + e['dur']:.2f})'[ve{k}]")
        chain, n = f"[ve{k}]", n + 1
    fps = f",fps={cfg.get('max_fps', 60)}" if src["fps"] > cfg.get("max_fps", 60) + 0.5 else ""
    vf += f";{chain}ass='{_escape_filter_path(ass)}':fontsdir='{_escape_filter_path(fonts)}'{fps},format=yuv420p[v]"

    lufs = cfg.get("loudness_lufs", -14)
    if src["has_audio"]:
        af = f"[0:a]loudnorm=I={lufs}:TP=-1.5:LRA=11[voice]"
    else:
        inputs += ["-f", "lavfi", "-t", f"{dur:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
        af, n = f"[{n}:a]anull[voice]", n + 1
    hits = []
    for k, s in enumerate(plan.get("sfx", [])):
        inputs += ["-i", str(effects.sfx_file(s["kind"]))]
        ms = int(s["t"] * 1000)
        af += f";[{n}:a]adelay={ms}|{ms},volume={eff.get('sfx_volume', 0.35)}[s{k}]"
        hits.append(f"[s{k}]")
        n += 1
    af += (f";[voice]{''.join(hits)}amix=inputs={1 + len(hits)}:duration=first:normalize=0[a]" if hits
           else ";[voice]anull[a]")
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", f"{vf};{af}", "-map", "[v]", "-map", "[a]",
           "-t", f"{dur:.2f}"]
    _encode(cmd, out, cfg)
    if base != raw:
        pathlib.Path(base).unlink(missing_ok=True)
    return {"path": str(out), "duration": dur, "plan": used}


def _encode(cmd, out, cfg):
    tail = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(out)]
    cpu = ["-c:v", "libx264", "-preset", cfg.get("preset", "medium"), "-crf", str(cfg.get("crf", 20))]
    if use_nvenc(cfg):
        gpu = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(cfg.get("crf", 20) + 1), "-b:v", "0"]
        try:
            run(cmd + gpu + tail)
            return
        except RuntimeError as e:
            import datetime as dt
            from . import db
            from .util import iso, utcnow
            # One failure shouldn't mean CPU forever: pause the GPU for 6 hours, then try it again.
            db.kv_set("nvenc_ok", None)
            db.kv_set("nvenc_pause_until", iso(utcnow() + dt.timedelta(hours=6)))
            db.kv_set("nvenc_last_error", str(e)[-300:])
            db.log(None, f"GPU video encoding failed ({str(e)[-160:]}); using the CPU for 6 hours", "warn")
    run(cmd + cpu + tail)


def use_nvenc(cfg):
    """encoder: auto uses the NVIDIA encoder when FFmpeg has it and it hasn't failed before."""
    choice = cfg.get("encoder", "auto")
    if choice == "cpu":
        return False
    from . import db
    from .util import parse_iso, utcnow
    pause = db.kv_get("nvenc_pause_until")
    if choice == "auto" and pause and utcnow() < parse_iso(pause):
        return False
    import subprocess
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "h264_nvenc" in out


def _pick_music(cfg):
    d = cfg.get("music_dir")
    if not d:
        return None
    p = config.path(d)
    tracks = [t for t in p.glob("*") if t.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg")] if p.is_dir() else []
    return str(random.choice(tracks)) if tracks else None
