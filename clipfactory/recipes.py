"""Campaign formats ("recipes"): clips a campaign wants BUILT from the assets it gives, not cut from a long video.

A recipe is saved on the campaign (`recipe` in its notes). The first one is "ai_reveal", the GTA VI Lucia x
creatorinseconds format "Idea 1: leaked GTA 6 footage": the campaign's own AI Lucia clips, a GTA-style HUD and the
hook "leaked GTA 6 footage of Lucia is actually insane", then around 5 s the text flips to "wait… this isn't GTA 6.
she's AI", then "you can make your own in seconds", and the campaign's end card on top of the last 3 seconds with a
Screen blend (the black disappears, the logo stays, moved down so it doesn't cover her face). 8-20 s.

Each footage video becomes one clip (a short one gets a second clip after it to reach the minimum length); the
metadata, AI check, review and posting steps are the usual ones.
"""
import json
import pathlib
import random

from . import campaigns, config, db
from .util import ffprobe, iso, utcnow

AI_REVEAL = {
    "name": "ai_reveal",
    "hook": ["leaked GTA 6 footage of Lucia is actually insane", "GTA 6 Lucia gameplay looks way too real",
             "this GTA 6 leak of Lucia is crazy", "nobody is talking about this GTA 6 Lucia footage"],
    "reveal": ["wait... this isn't GTA 6. she's AI", "plot twist: this isn't GTA 6. she's AI",
               "wait... she's not even in the game. she's AI"],
    "cta": ["you can make your own in seconds", "you can make your own AI Lucia in seconds",
            "people are making money with AI girls like her"],
    "reveal_at": 5.0,      # seconds
    "end_card_s": 3.0,     # the end card covers the last 3 seconds
    "end_card_shift": 560,  # px down by default; moved per clip off faces and text (card_shift)
    "hud": True,
}


def get(c):
    r = campaigns._extra(c).get("recipe")
    if isinstance(r, dict) and r.get("name") == "ai_reveal":
        return {**AI_REVEAL, **r}
    return None


def set_recipe(cid, end_card, **over):
    extra = campaigns._extra(campaigns.get(cid))
    extra["recipe"] = {"name": "ai_reveal", "end_card": str(end_card), **over}
    extra["captions"] = "off"   # the recipe's own lines are the only text on screen
    db.x("UPDATE campaigns SET notes=? WHERE id=?", json.dumps(extra), cid)
    # clips skipped earlier as "too short" are this format's footage
    db.x("UPDATE videos SET status='waiting', error=NULL WHERE campaign_id=? AND status='skipped' AND error='too short'",
         cid)


# ---------- stage: a footage video → one planned clip ----------

def plan_videos(c, limit=3):
    """New footage videos of a recipe campaign become moments (the whole clip), no AI watching needed."""
    r = get(c)
    for v in db.q("SELECT * FROM videos WHERE campaign_id=? AND status='new' AND local_path IS NOT NULL "
                  "ORDER BY created_at LIMIT ?", c["id"], limit):
        try:
            dur = v["duration_s"] or ffprobe(v["local_path"])["duration"]
        except Exception as e:
            db.x("UPDATE videos SET status='failed', error=? WHERE id=? AND campaign_id=?", str(e)[:300], v["id"], c["id"])
            continue
        rnd = random.Random(f"{c['id']}:{v['id']}")
        hook = rnd.choice(r["hook"])
        db.x("""INSERT INTO moments(video_id, campaign_id, start_s, end_s, score, category, hook, why, status,
                                    created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
             v["id"], c["id"], 0.0, min(dur, float(c["max_len"] or 20)), float(r.get("score", 9)), r["name"], hook,
             "campaign format: AI reveal", "found", iso(utcnow()))
        db.x("UPDATE videos SET status='analyzed', analyzed_at=? WHERE id=? AND campaign_id=?",
             iso(utcnow()), v["id"], c["id"])


def _partner(c, m):
    """Another footage clip to follow a short one (so the clip reaches the campaign's minimum length)."""
    rows = db.q("SELECT local_path FROM videos WHERE campaign_id=? AND id != ? AND local_path IS NOT NULL",
                c["id"], m["video_id"])
    rows = [r["local_path"] for r in rows if r["local_path"] and pathlib.Path(r["local_path"]).exists()]
    return random.Random(m["id"]).choice(rows) if rows else None


def _ass(path, lines, dur, r, font, rnd=random):
    """Text on screen: hook (+ HUD) → reveal → call to action, all high in the frame, away from the end card."""
    from .render import _ass_time
    t_rev = min(r["reveal_at"], max(2.5, dur - r["end_card_s"] - 2.0))
    t_cta = max(t_rev + 1.5, dur - r["end_card_s"] - 0.5)
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
            "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Hook,{font},74,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,3,8,70,70,250,1\n"
            f"Style: Rev,{font},78,&H0000F0FF,&H0000F0FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,8,3,8,70,70,250,1\n"
            f"Style: Hud,{font},58,&H0047C247,&H0047C247,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,5,0,9,40,50,70,1\n"
            "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ev = [f"Dialogue: 1,{_ass_time(0)},{_ass_time(t_rev)},Hook,,0,0,0,,{{\\fad(120,80)}}{lines['hook']}",
          f"Dialogue: 1,{_ass_time(t_rev)},{_ass_time(t_cta)},Rev,,0,0,0,,{{\\fad(80,80)\\t(0,180,\\fscx112\\fscy112)"
          f"\\t(180,320,\\fscx100\\fscy100)}}{lines['reveal']}",
          f"Dialogue: 1,{_ass_time(t_cta)},{_ass_time(dur)},Hook,,0,0,0,,{{\\fad(100,0)}}{lines['cta']}"]
    if r.get("hud"):   # a GTA-style HUD while it "looks like" gameplay: money + wanted stars, a minimap
        ev += [f"Dialogue: 0,{_ass_time(0)},{_ass_time(t_rev)},Hud,,0,0,0,,$1,048,{rnd.randint(100, 999)}",
               f"Dialogue: 0,{_ass_time(0)},{_ass_time(t_rev)},Hud,,0,0,0,,{{\\an9\\pos(1040,150)\\c&HFFFFFF&\\fs50}}"
               "★★★☆☆",
               f"Dialogue: 0,{_ass_time(0)},{_ass_time(t_rev)},Hud,,0,0,0,,{{\\an7\\pos(50,1560)\\bord6\\3c&HFFFFFF&"
               "\\1c&H1E1E1E&\\1a&H50&\\p1}m 150 0 b 233 0 300 67 300 150 b 300 233 233 300 150 300 b 67 300 0 233 0 "
               "150 b 0 67 67 0 150 0{\\p0}",
               f"Dialogue: 0,{_ass_time(0)},{_ass_time(t_rev)},Hud,,0,0,0,,{{\\an7\\pos(185,1690)\\bord0\\1c&HFFFFFF&"
               "\\p1}m 15 0 l 30 30 l 15 22 l 0 30{\\p0}"]
    pathlib.Path(path).write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


FIT = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v0]"
TOP_TEXT = 470   # our lines sit above this (y px); the card never goes there


def _card_band(card):
    """(top, bottom) px of what's visible on the end card (its logo + text), on the 1080x1920 canvas."""
    from . import layout
    frames = layout.sample_frames(card, "[0:v]scale=1080:1920,setsar=1[v0]", 3.0, n=3, width=270)
    if not frames:
        return 440, 980
    import numpy as np
    lit = np.max(np.stack([f.max(axis=2) for f in frames]), axis=0).max(axis=1) > 50
    ys = np.nonzero(lit)[0]
    if not len(ys):
        return 440, 980
    k = 1920 / frames[0].shape[0]
    return int(ys[0] * k), int(ys[-1] * k)


def _face_rows(frames):
    """Share of frames with a face in each of layout.BANDS bands (YuNet), or zeros without OpenCV."""
    import numpy as np
    from . import effects, layout
    rows = np.zeros(layout.BANDS)
    try:
        import cv2
        model = effects._yunet()
        if not model or not hasattr(cv2, "FaceDetectorYN") or not frames:
            return rows
        h, w = frames[0].shape[:2]
        det = cv2.FaceDetectorYN.create(str(model), "", (w, h), 0.6)
        for f in frames:
            _, faces = det.detect(f)
            hit = np.zeros(layout.BANDS, bool)
            for x, y, fw, fh in [tuple(map(float, face[:4])) for face in (faces if faces is not None else [])]:
                hit[max(0, int(y / h * layout.BANDS)):min(layout.BANDS, int((y + fh * 1.3) / h * layout.BANDS) + 1)] = True
            rows += hit
    except Exception:
        return rows
    return rows / max(1, len(frames))


def card_shift(parts, start, card, default=560):
    """How far to move the end card (px, + = down) so its logo lands on no face and no text already in the
    picture (a clip's own caption), and below our lines. Looks at the footage under the last seconds."""
    from . import layout
    try:
        offset, src = start, parts[-1]
        for p in parts[:-1]:   # which part plays under the end card, and from when
            d = ffprobe(p)["duration"]
            if offset < d:
                src = p
                break
            offset -= d
        span = max(1.0, ffprobe(src)["duration"] - offset)
        frames = layout.sample_frames(src, FIT.replace("[0:v]", f"[0:v]trim=start={offset:.2f},setpts=PTS-STARTPTS,"),
                                      span, n=6)
        text, face = layout.text_rows(frames), _face_rows(frames)
        top, bottom = _card_band(card)
    except Exception:
        return default
    best, score = default, None
    for shift in range(-400, 901, 40):
        a, b = top + shift, bottom + shift
        if a < TOP_TEXT or b > 1880:
            continue
        lo, hi = int((a - 40) / 1920 * layout.BANDS), min(layout.BANDS, int((b + 40) / 1920 * layout.BANDS) + 1)
        s = 3 * float(text[lo:hi].max(initial=0)) + 2 * float(face[lo:hi].max(initial=0)) + abs(shift - default) / 4000
        if score is None or s < score:
            best, score = shift, s
    return best


def render_clip(c, clip, m):
    """Build the finished clip. Returns {"path", "duration", "plan"} like render.render."""
    from . import render, styles
    r = get(c)
    raw = clip["raw_path"]
    parts = [raw]
    lo, hi = float(c["min_len"] or 8), float(c["max_len"] or 20)
    dur = ffprobe(raw)["duration"]
    if dur < lo:
        extra = _partner(c, m)
        if extra:
            parts.append(extra)
            dur += ffprobe(extra)["duration"]
    dur = round(min(dur, hi), 2)
    rnd = random.Random(f"lines:{m['id']}:{clip['id']}")
    lines = {"hook": m["hook"] or rnd.choice(r["hook"]), "reveal": rnd.choice(r["reveal"]),
             "cta": rnd.choice(r["cta"])}
    out = pathlib.Path(raw.replace("_raw.mp4", "_final.mp4"))
    ass = out.with_suffix(".ass")
    fonts = config.path(config.section("render").get("fonts_dir", "assets/fonts"))
    styles.ensure_fonts(fonts)
    _ass(ass, lines, dur, r, "Montserrat Black", rnd)
    inputs, vf = [], []
    for i, p in enumerate(parts):
        inputs += ["-i", str(p)]
        vf.append(f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30[v{i}]")
        has_a = ffprobe(p).get("has_audio")
        vf.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]" if has_a else
                  f"anullsrc=r=48000:cl=stereo,atrim=0:{ffprobe(p)['duration']:.2f}[a{i}]")
    n = len(parts)
    vf.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[base][aud]")
    ec = r["end_card"]
    ecn = n
    inputs += ["-i", str(ec)]
    start = max(0.0, dur - r["end_card_s"])
    shift = card_shift(parts, start, ec, int(r.get("end_card_shift", 0)))
    move = (f"crop=1080:{1920 - shift}:0:0,pad=1080:1920:0:{shift}:black" if shift >= 0 else
            f"crop=1080:{1920 + shift}:0:{-shift},pad=1080:1920:0:0:black")
    vf.append(f"[{ecn}:v]scale=1080:1920,setsar=1,fps=30,{move},tpad=start_duration={start:.2f}:color=black:"
              f"stop_mode=add:stop_duration=6:color=black,format=gbrp[ec]")
    vf.append(f"[base]trim=0:{dur:.2f},setpts=PTS-STARTPTS,format=gbrp[b]")
    vf.append("[b][ec]blend=all_mode=screen:shortest=1,format=yuv420p,"
              f"ass='{render._escape_filter_path(ass)}':fontsdir='{render._escape_filter_path(fonts)}'[v]")
    vf.append(f"[aud]atrim=0:{dur:.2f},asetpts=PTS-STARTPTS,"
              f"loudnorm=I={config.section('render').get('loudness_lufs', -14)}:TP=-1.5:LRA=11[a]")
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(vf), "-map", "[v]", "-map", "[a]",
           "-t", f"{dur:.2f}"]
    render._encode(cmd, out, config.section("render"))
    ass.unlink(missing_ok=True)
    plan = {"recipe": r["name"], "lines": lines, "parts": len(parts), "end_card_from": round(start, 2),
            "end_card_shift": shift}
    return {"path": str(out), "duration": dur, "plan": plan}

