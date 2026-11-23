"""Understand the whole video first, then plan the clips.

1. WATCH: Gemini watches the whole video (YouTube links directly, local/Dropbox/Drive files after uploading them),
   part by part, and writes a report: what it is about, every scene (what's shown, who talks, energy), the
   highlights, what to skip, text or logos already on screen. Without Gemini: transcript + scene cuts.
2. THINK: the "director" (Claude when its key is set, else the free AI) reads that report, the timestamped
   transcript and the campaign guide, and plans the clips: where each starts and ends, the hook, the title,
   and its edit (zooms, flashes, emojis, words to blow up, what to cut).
3. The report and the plan are posted on Discord, so you can see what the AI understood and decided.
   The moments (with their edit plans) then go through the usual cut -> edit -> review steps.
"""
import json
import re
import subprocess

from . import config, db, llm, notify, transcribe
from .util import fmt_ts, iso, parse_ts, utcnow

WATCH_PROMPT = """You are watching part of a {kind} ({start} to {end} of {total}) that will be cut into short
vertical clips for US viewers. Campaign topic: {topic}.{rules}

Watch carefully, both picture and sound. Describe what is really there. Times are absolute timestamps in the
video (MM:SS or HH:MM:SS).

Return JSON only:
{{"summary": "2-3 sentences: what this part is",
  "language": "two-letter code of the main spoken language",
  "on_screen_text": true if captions/subtitles are already burned into the picture,
  "logos": "watermarks or logos you see, or empty",
  "scenes": [{{"start": "MM:SS", "end": "MM:SS", "shows": "what we see", "who": "who talks", "energy": 1-10,
               "usable": "great | ok | skip", "note": "why"}}],
  "highlights": [{{"start": "MM:SS", "end": "MM:SS", "what": "what happens", "why": "why viewers would stop scrolling",
                   "energy": 1-10}}],
  "skip": [{{"start": "MM:SS", "end": "MM:SS", "why": "ads, dead air, off-topic, unclear, rule-breaking"}}]}}
"""

PLAN_PROMPT = """You are the editor of a clipping channel. A {kind} has been watched and described below. Plan the
best short vertical clips from it for US viewers on YouTube Shorts, Instagram Reels and TikTok.

Campaign topic: {topic}. Target viewers: {audience}.{rules}

Rules for every clip:
- {min_len}-{max_len} seconds; start ON the action or the strongest line (no setup), end right after the payoff.
- Each clip must make sense on its own and must not overlap another clip.
- Follow the campaign rules and guide above exactly. Skip anything they forbid.
- Only plan clips that are genuinely strong (score 1-10, be strict). At most {limit} clips.
- Edit plan times are seconds from the START OF THE CLIP. Keep effects purposeful: punch zooms on the 1-3
  biggest beats, at most one flash, emojis only where they fit, cut dead air inside the clip.

Video report (what was seen and heard):
{report}

Timestamped transcript:
{transcript}

Return JSON only:
{{"thinking": "3-5 sentences: what this video is and how you decided",
  "clips": [{{"start": "MM:SS or HH:MM:SS", "end": "...", "score": 1-10,
              "category": "funny | crazy | fail | clutch | reaction | shock | story | insight | hot_take",
              "hook": "max 6 words on screen at the start", "why": "why it will do well",
              "edit": {{"zooms": [{{"t": seconds, "scale": 1.1-1.3}}], "flashes": [seconds],
                        "emojis": [{{"t": seconds, "emoji": "😂"}}], "emphasis_words": ["insane"],
                        "cuts": [[seconds, seconds]]}}}}]}}
"""


# ---------- step 1: watch ----------

def _ts(v, base, span):
    s = parse_ts(v)
    if base > 0 and s < base - 5 and s <= span + 5:   # answered relative to the part: shift into place
        s += base
    return round(s, 2)


def _watch_validator(base, span):
    def validate(d):
        if not isinstance(d, dict) or not str(d.get("summary") or "").strip():
            raise llm.Invalid("no summary")
        out = {"summary": str(d["summary"])[:600], "language": str(d.get("language") or "en")[:2].lower(),
               "on_screen_text": bool(d.get("on_screen_text")), "logos": str(d.get("logos") or "")[:120],
               "scenes": [], "highlights": [], "skip": []}
        for key, fields in (("scenes", ("shows", "who", "usable", "note")), ("highlights", ("what", "why")),
                            ("skip", ("why",))):
            for item in (d.get(key) or [])[:80]:
                try:
                    s, e = _ts(item["start"], base, span), _ts(item["end"], base, span)
                except (KeyError, ValueError, TypeError):
                    continue
                if e <= s:
                    continue
                row = {"start": s, "end": e, **{f: str(item.get(f) or "")[:200] for f in fields}}
                if "energy" in item:
                    try:
                        row["energy"] = max(1, min(10, int(item["energy"])))
                    except (TypeError, ValueError):
                        pass
                out[key].append(row)
        llm.require_english(out["summary"])
        return out
    return validate


def scene_cuts(path, threshold=0.35):
    """Times (s) where the picture changes shot, from FFmpeg."""
    try:
        r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-vf",
                            f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=1800)
        return [round(float(t), 2) for t in re.findall(r"pts_time:([\d.]+)", r.stderr)]
    except Exception:
        return []


def watch(v, c):
    """The report for the whole video. Returns (report, how)."""
    cfg = config.section("analyst")
    total = v["duration_s"] or 0
    kind = "livestream VOD" if v["is_stream"] else "video"
    is_youtube = bool(v["url"]) and ("youtube.com" in v["url"] or "youtu.be" in v["url"])
    if llm.can_watch_video() and (is_youtube or v["local_path"]):
        part = cfg.get("part_minutes", 15) * 60
        merged = {"summaries": [], "language": [], "on_screen_text": False, "logos": "", "scenes": [],
                  "highlights": [], "skip": []}
        start = 0.0
        while start < total:
            end = min(total, start + part)
            video = {"start": start, "end": end, "fps": cfg.get("fps")}
            video.update({"url": v["url"]} if is_youtube else {"path": v["local_path"]})
            prompt = WATCH_PROMPT.format(kind=kind, start=fmt_ts(start), end=fmt_ts(end), total=fmt_ts(total),
                                         topic=c["topic"], rules=llm.rules_block(c))
            part_report, provider = llm.complete_json("video_watch", prompt, _watch_validator(start, end - start),
                                                      video=video)
            merged["summaries"].append(part_report["summary"])
            merged["language"].append(part_report["language"])
            merged["on_screen_text"] |= part_report["on_screen_text"]
            merged["logos"] = merged["logos"] or part_report["logos"]
            for k in ("scenes", "highlights", "skip"):
                merged[k] += part_report[k]
            db.log(c["id"], f"{provider} watched {fmt_ts(start)}-{fmt_ts(end)} of {v['title']}")
            start = end
        langs = merged.pop("language")
        merged["language"] = max(set(langs), key=langs.count) if langs else "en"
        merged["summary"] = " ".join(merged.pop("summaries"))[:1500]
        return merged, "watched by Gemini"
    # No video-watching AI: transcript + where the shots change
    cuts = scene_cuts(v["local_path"]) if v["local_path"] else []
    return {"summary": "", "language": "en", "on_screen_text": False, "logos": "", "scenes": [
        {"start": a, "end": b, "shows": "", "who": "", "usable": "", "note": ""}
        for a, b in zip([0.0] + cuts, cuts + [total])], "highlights": [], "skip": []}, "transcript + scene cuts"


# ---------- step 2: think ----------

def _transcript(v, limit_chars=60000):
    from .moments import _audio_file
    segs = transcribe.segments(_audio_file(v))
    text = "\n".join(f"[{fmt_ts(s['start'])}] {s['text'].strip()}" for s in segs)
    return text[:limit_chars], segs


def _plan_validator(c, total):
    def validate(d):
        if not isinstance(d, dict) or not isinstance(d.get("clips"), list):
            raise llm.Invalid("no clips list")
        clips = []
        for x in d["clips"][:c["max_moments_per_video"] * 2]:
            try:
                s, e = parse_ts(x["start"]), parse_ts(x["end"])
            except (KeyError, ValueError, TypeError):
                continue
            if e <= s or s < 0 or (total and s > total):
                continue
            hook, why = str(x.get("hook") or "")[:60], str(x.get("why") or "")[:300]
            llm.require_english(hook, why)
            cat = str(x.get("category") or "").lower()
            from .moments import CATEGORIES
            clips.append({"start": s, "end": e, "score": float(x.get("score") or 0), "hook": hook, "why": why,
                          "category": cat if cat in CATEGORIES else "reaction",
                          "edit": x.get("edit") if isinstance(x.get("edit"), dict) else {}})
        return {"thinking": str(d.get("thinking") or "")[:800], "clips": clips}
    return validate


def _report_text(report, limit=40000):
    lines = [f"Summary: {report.get('summary', '')}",
             f"Text already on screen: {'yes' if report.get('on_screen_text') else 'no'}; logos: {report.get('logos') or 'none'}"]
    for key, title in (("highlights", "HIGHLIGHTS"), ("scenes", "SCENES"), ("skip", "SKIP")):
        rows = report.get(key) or []
        if rows:
            lines.append(title)
            for r in rows:
                desc = " | ".join(str(r.get(f)) for f in ("shows", "what", "who", "why", "usable", "note") if r.get(f))
                energy = f" energy {r['energy']}" if r.get("energy") else ""
                lines.append(f"- {fmt_ts(r['start'])}-{fmt_ts(r['end'])}{energy}: {desc}")
    return "\n".join(lines)[:limit]


def plan(v, c, report):
    transcript, _ = _transcript(v)
    prompt = PLAN_PROMPT.format(kind="livestream VOD" if v["is_stream"] else "video", topic=c["topic"],
                                audience=c["audience"], rules=llm.rules_block(c), min_len=int(c["min_len"]),
                                max_len=int(c["max_len"]), limit=c["max_moments_per_video"],
                                report=_report_text(report), transcript=transcript or "(no speech)")
    return llm.complete_json("video_plan", prompt, _plan_validator(c, v["duration_s"] or 0))


# ---------- the whole step ----------

def to_effects(edit, words_hint=None):
    """The director's edit -> an effects plan (times stay clip-relative; emphasis by word is resolved later)."""
    e = edit or {}
    out = {"zooms": [{"t": z.get("t"), "scale": z.get("scale", 1.15), "dur": z.get("dur", 0.6)}
                     for z in e.get("zooms") or [] if isinstance(z, dict)],
           "flashes": e.get("flashes") or [], "emojis": [{**m, "dur": m.get("dur", 0.9)} for m in e.get("emojis") or []
                                                        if isinstance(m, dict)],
           "cuts": e.get("cuts") or [], "emphasis_words": [str(w).lower() for w in e.get("emphasis_words") or []][:6],
           "sfx": [{"t": z.get("t", 0) - 0.08, "kind": "whoosh"} for z in e.get("zooms") or [] if isinstance(z, dict)]}
    return out


def analyze(v, c):
    report, how = watch(v, c)
    if config.section("moments").get("english_only", True) and report.get("language", "en") != "en":
        lang = report["language"]
        db.x("UPDATE videos SET status='skipped', route=?, analyzed_at=?, error=? WHERE id=? AND campaign_id=?",
             "analyst", iso(utcnow()), f"not English ({lang})", v["id"], c["id"])
        notify.send("progress", f"Skipped \"{v['title']}\" ({c['name']}): the speech is not English ({lang}).",
                    campaign_id=c["id"])
        return []
    planned, provider = plan(v, c, report)
    strong = [x for x in planned["clips"] if x["score"] >= c["min_score"]]
    taken = db.q("SELECT start_s, end_s FROM moments WHERE video_id=? AND campaign_id=?", v["id"], c["id"])
    kept = []
    for x in sorted(strong, key=lambda x: -x["score"]):
        if any(x["start"] < t["end_s"] and x["end"] > t["start_s"] for t in taken) or \
                any(x["start"] < k["end"] and x["end"] > k["start"] for k in kept):
            continue
        kept.append(x)
        if len(kept) >= c["max_moments_per_video"]:
            break
    for x in sorted(kept, key=lambda x: x["start"]):
        db.x("""INSERT INTO moments(video_id, campaign_id, start_s, end_s, score, category, hook, why, plan, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", v["id"], c["id"], x["start"], x["end"], x["score"], x["category"],
             x["hook"], x["why"], json.dumps(to_effects(x["edit"]), ensure_ascii=False), iso(utcnow()))
    db.x("UPDATE videos SET status='analyzed', route=?, analyzed_at=?, error=NULL, report=? WHERE id=? AND campaign_id=?",
         "analyst", iso(utcnow()), json.dumps(report, ensure_ascii=False)[:200000], v["id"], c["id"])
    db.log(c["id"], f"{v['title']}: {len(report.get('scenes', []))} scenes, {len(kept)} clips planned by {provider}")
    post_report(v, c, report, how, planned, kept, provider)
    return kept


def post_report(v, c, report, how, planned, kept, provider):
    lines = [f"🧠 **Video understood**: \"{v['title'][:80]}\" · {c['name']}",
             f"{how} · {len(report.get('scenes', []))} scenes · {len(report.get('highlights', []))} highlights · "
             f"{len(report.get('skip', []))} parts to skip"]
    if report.get("summary"):
        lines.append(f"About: {report['summary'][:400]}")
    if report.get("on_screen_text"):
        lines.append("Text is already on screen, so our captions stay off.")
    if planned.get("thinking"):
        lines.append(f"Editor ({provider}): {planned['thinking'][:500]}")
    if kept:
        lines.append(f"**{len(kept)} clip(s) planned:**")
        lines += [f"{i}. {fmt_ts(x['start'])}-{fmt_ts(x['end'])} · {x['score']:.0f}/10 · {x['hook'] or x['category']}"
                  for i, x in enumerate(sorted(kept, key=lambda x: x["start"]), 1)]
        lines.append("Cutting and editing them now; they come here for review.")
    else:
        lines.append(f"No clip scored {c['min_score']:.0f}/10 or more, so none were made from this video.")
    notify.send("progress", "\n".join(lines)[:1900], campaign_id=c["id"])
