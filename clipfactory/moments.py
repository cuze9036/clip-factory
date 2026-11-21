"""Stage 2: ask the AI where the funny, crazy and viral moments are."""
import subprocess

import numpy as np

from . import config, db, llm, transcribe
from .util import fmt_ts, iso, parse_ts, utcnow

CATEGORIES = {"funny", "crazy", "fail", "clutch", "reaction", "shock", "story", "insight", "hot_take"}


def _validator(chunk_start, chunk_end, limit):
    def validate(data):
        if isinstance(data, dict):
            data = data.get("moments") or data.get("items") or []
        if not isinstance(data, list):
            raise llm.Invalid("expected a list")
        out = []
        for m in data[: limit * 2]:
            s, e = parse_ts(m["start"]), parse_ts(m["end"])
            # Some models answer relative to the chunk; shift those into place.
            if chunk_start > 0 and s < chunk_start - 5 and s + chunk_start <= chunk_end + 5:
                s, e = s + chunk_start, e + chunk_start
            if e <= s or s < chunk_start - 30 or s > chunk_end + 5:
                continue
            hook, why = str(m.get("hook", ""))[:60], str(m.get("why", ""))[:300]
            llm.require_english(hook, why)
            cat = str(m.get("category", "")).lower()
            lang = str(m.get("language") or "en").strip().lower()[:2]
            out.append({"start": s, "end": e, "score": float(m.get("score", 0)), "language": lang,
                        "category": cat if cat in CATEGORIES else "reaction", "hook": hook, "why": why})
        return out
    return validate


def _shape(moments, c, duration):
    """Clamp length, add preroll, drop overlaps and weak picks."""
    pre = config.section("moments").get("preroll_s", 1.0)
    lo, hi = c["min_len"], c["max_len"]
    shaped = []
    for m in sorted(moments, key=lambda m: -m["score"]):
        if m["score"] < c["min_score"]:
            continue
        s = max(0.0, m["start"] - pre)
        e = m["end"]
        if e - s < lo:
            e = s + lo
        if e - s > hi:
            e = s + hi
        if duration:
            e = min(e, duration)
        if e - s < lo * 0.75:
            continue
        if any(s < o["end"] and e > o["start"] for o in shaped):
            continue
        shaped.append({**m, "start": round(s, 2), "end": round(e, 2)})
        if len(shaped) >= c["max_moments_per_video"]:
            break
    return sorted(shaped, key=lambda m: m["start"])


def via_gemini(v, c):
    cfg = config.section("moments")
    chunk = cfg.get("chunk_minutes", 30) * 60
    total = v["duration_s"] or 0
    kind = "livestream VOD" if v["is_stream"] else "video"
    found, provider = [], None
    start = 0.0
    while start < total:
        end = min(total, start + chunk)
        prompt = llm.MOMENTS_VIDEO_PROMPT.format(
            kind=kind, start=fmt_ts(start), end=fmt_ts(end), total=fmt_ts(total), topic=c["topic"],
            audience=c["audience"], rules=llm.rules_block(c), min_len=c["min_len"], max_len=c["max_len"],
            limit=cfg.get("max_per_chunk", 4))
        data, provider = llm.complete_json(
            "moments_video", prompt, _validator(start, end, cfg.get("max_per_chunk", 4)),
            video={"url": v["url"], "start": start, "end": end, "fps": cfg.get("fps")})
        found.extend(data)
        db.log(c["id"], f"{provider} watched {fmt_ts(start)}-{fmt_ts(end)} of {v['title']}: {len(data)} moments")
        start = end
    return found, provider


def _audio_file(v):
    if v["local_path"]:
        return v["local_path"]
    from yt_dlp import YoutubeDL
    out = config.data_dir() / "audio" / v["id"]
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = list(out.parent.glob(v["id"] + ".*"))
    if existing:
        return str(existing[0])
    opts = {"format": "bestaudio/best", "outtmpl": str(out) + ".%(ext)s", "quiet": True, "noprogress": True}
    cookies = config.section("download").get("cookies_file")
    if cookies:
        opts["cookiefile"] = str(config.path(cookies))
    with YoutubeDL(opts) as y:
        y.download([v["url"]])
    return str(next(out.parent.glob(v["id"] + ".*")))


def loudness_per_second(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    n = len(a) // 8000
    if n == 0:
        return np.zeros(0)
    rms = np.sqrt(np.mean(a[: n * 8000].reshape(n, 8000) ** 2, axis=1)) + 1e-6
    return 20 * np.log10(rms / 32768)


def via_transcript(v, c):
    audio = _audio_file(v)
    segs = transcribe.segments(audio)
    loud = loudness_per_second(audio)
    total = v["duration_s"] or len(loud)
    base = float(np.median(loud)) if len(loud) else -40.0
    windows = []
    for w0 in range(0, int(total), 30):
        w1 = w0 + 60
        lines = [s for s in segs if w0 <= s["start"] < w1]
        if not lines:
            continue
        spike = float(np.percentile(loud[w0:w1], 95) - base) if len(loud[w0:w1]) else 0.0
        words = sum(len(s["text"].split()) for s in lines)
        windows.append((spike * 2 + words / 20, w0, w1, spike, lines))
    top = sorted(windows, key=lambda w: -w[0])[:12]
    blocks = []
    for _, w0, w1, spike, lines in sorted(top, key=lambda w: w[1]):
        tag = f" (loudness spike +{spike:.0f} dB)" if spike > 12 else ""
        body = "\n".join(f"[{fmt_ts(s['start'])}] {s['text'].strip()}" for s in lines)
        blocks.append(f"--- Window {fmt_ts(w0)}-{fmt_ts(w1)}{tag}\n{body}")
    if not blocks:
        return [], None
    prompt = llm.MOMENTS_TRANSCRIPT_PROMPT.format(
        kind="livestream VOD" if v["is_stream"] else "video", total=fmt_ts(total), topic=c["topic"],
        audience=c["audience"], rules=llm.rules_block(c), min_len=c["min_len"],
        max_len=c["max_len"], limit=c["max_moments_per_video"], windows="\n\n".join(blocks))
    return llm.complete_json("moments_transcript", prompt, _validator(0, total, c["max_moments_per_video"]))


def analyze(v, c):
    route = config.section("moments").get("route", "auto")
    # Gemini can only watch public YouTube links; Twitch, Kick and local files use the transcript route.
    is_youtube = bool(v["url"]) and ("youtube.com" in v["url"] or "youtu.be" in v["url"])
    use_video = route in ("auto", "gemini") and is_youtube and llm.can_watch_video()
    raw, provider, used = [], None, None
    if route == "gemini" and not use_video:
        raise RuntimeError("route is 'gemini' but this video can't be watched by Gemini")
    if use_video:
        try:
            raw, provider = via_gemini(v, c)
            used = "gemini-video"
        except Exception as e:
            if route == "gemini":
                raise
            db.log(c["id"], f"Video route failed, using transcript: {str(e)[:200]}", "warn")
    if used is None:
        raw, provider = via_transcript(v, c)
        used = "transcript"
    if config.section("moments").get("english_only", True) and raw:
        other = [m["language"] for m in raw if m.get("language", "en") != "en"]
        if len(other) * 2 > len(raw):
            lang = max(set(other), key=other.count)
            db.x("UPDATE videos SET status='skipped', route=?, analyzed_at=?, error=? WHERE id=? AND campaign_id=?",
                 used, iso(utcnow()), f"not English ({lang})", v["id"], c["id"])
            from . import notify
            notify.send("progress", f"Skipped \"{v['title']}\" ({c['name']}): the speech is not English ({lang}). "
                        "Clips are only made from English videos for US viewers.", campaign_id=c["id"])
            db.log(c["id"], f"Skipped {v['title']}: not English ({lang})")
            return []
    picks = _shape(raw, c, v["duration_s"])
    for m in picks:
        db.x("""INSERT INTO moments(video_id, campaign_id, start_s, end_s, score, category, hook, why, created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""", v["id"], c["id"], m["start"], m["end"], m["score"], m["category"],
             m["hook"], m["why"], iso(utcnow()))
    db.x("UPDATE videos SET status='analyzed', route=?, analyzed_at=?, error=NULL WHERE id=? AND campaign_id=?",
         used, iso(utcnow()), v["id"], c["id"])
    db.log(c["id"], f"{len(picks)} moments kept from {v['title']} (route: {used}, model: {provider})")
    from . import notify
    if picks:
        best = max(m["score"] for m in picks)
        notify.send("progress", f"Found {len(picks)} moment(s) in \"{v['title']}\" ({c['name']}), best {best:.0f}/10. "
                    "Cutting and captioning them now; they'll show up here in about 5-15 minutes.", campaign_id=c["id"])
    else:
        notify.send("progress", f"No strong moments in \"{v['title']}\" ({c['name']}): nothing scored "
                    f"{c['min_score']:.0f}/10 or more, so no clips were made from it.", campaign_id=c["id"])
    return picks
