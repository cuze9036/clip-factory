"""LLM router: tries each configured provider in order, validates JSON, caches answers, enforces English."""
import hashlib
import json
import re
import time

from langdetect import DetectorFactory, detect_langs

from . import config, db
from .util import iso, utcnow

DetectorFactory.seed = 0


class LLMError(Exception):
    pass


class Invalid(Exception):
    pass


_cooldown = {}
_clients = {}


def providers(video=False):
    out = []
    for p in config.section("llm").get("providers") or []:
        p = dict(p)
        p["api_key"] = p.get("api_key") or config.env(p.get("api_key_env", ""), None)
        p["base_url"] = p.get("base_url") or config.env(p.get("base_url_env", ""), None)
        p["model"] = p.get("model") or config.env(p.get("model_env", ""), None)
        if not p["api_key"] or not p["model"]:
            continue
        if p["type"] == "openai" and not p["base_url"]:
            continue
        if video and not p.get("video"):
            continue
        if _cooldown.get(p["name"], 0) > time.time():
            continue
        out.append(p)
    return out


def can_watch_video():
    return bool(providers(video=True))


NON_LATIN = re.compile(r"[Ͱ-῿Ⰰ-ⷿ぀-￯]")


def is_english(text):
    if not text:
        return True
    if NON_LATIN.search(text):
        return False
    letters = re.sub(r"[#@]\w+|https?://\S+|[^A-Za-z\s']", " ", text)
    if len(letters.split()) < 6:
        return True
    try:
        return any(l.lang == "en" and l.prob > 0.3 for l in detect_langs(letters))
    except Exception:
        return True


def require_english(*texts):
    for t in texts:
        if not is_english(t):
            raise Invalid(f"not English: {t[:60]!r}")


def parse_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\[.*\]|\{.*\})", text, re.S)
        if not m:
            raise Invalid("no JSON in response")
        return json.loads(m.group(1))


def _gemini(p, prompt, video):
    from google import genai
    from google.genai import types

    client = _clients.get(p["name"]) or genai.Client(api_key=p["api_key"])
    _clients[p["name"]] = client
    parts = []
    if video:
        meta = {"start_offset": f"{int(video['start'])}s", "end_offset": f"{int(video['end'])}s"}
        if video.get("fps"):
            meta["fps"] = video["fps"]
        parts.append(types.Part(file_data=types.FileData(file_uri=video["url"]),
                                video_metadata=types.VideoMetadata(**meta)))
    parts.append(types.Part(text=prompt))
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json", temperature=0.4,
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW if video else None,
    )
    r = client.models.generate_content(model=p["model"], contents=types.Content(role="user", parts=parts), config=cfg)
    return r.text


def _openai(p, prompt):
    from openai import OpenAI

    client = _clients.get(p["name"]) or OpenAI(base_url=p["base_url"], api_key=p["api_key"], timeout=120)
    _clients[p["name"]] = client
    msgs = [{"role": "system", "content": "Reply with valid JSON only, in American English."},
            {"role": "user", "content": prompt}]
    try:
        r = client.chat.completions.create(model=p["model"], messages=msgs, temperature=0.4,
                                           response_format={"type": "json_object"})
    except Exception as e:
        if "response_format" not in str(e) and "json" not in str(e).lower():
            raise
        r = client.chat.completions.create(model=p["model"], messages=msgs, temperature=0.4)
    return r.choices[0].message.content


def _rate_limited(e):
    s = str(e)
    return "429" in s or "RESOURCE_EXHAUSTED" in s or "rate limit" in s.lower() or "quota" in s.lower()


def complete_json(task, prompt, validate, video=None):
    """Ask the first working provider; returns (data, provider_name)."""
    key = hashlib.sha256(json.dumps([task, prompt, video], sort_keys=True).encode()).hexdigest()
    hit = db.one("SELECT provider, response FROM llm_cache WHERE key=?", key)
    if hit:
        try:
            return validate(json.loads(hit["response"])), hit["provider"] + " (cached)"
        except Invalid:
            pass
    errors = []
    tried = providers(video=bool(video))
    if all(p.get("optional") for p in tried):
        errors.append("no LLM provider configured (set GEMINI_API_KEY or FREE_LLM_* in .env)")
    for p in tried:
        for attempt in range(2):
            try:
                text = _gemini(p, prompt, video) if p["type"] == "gemini" else _openai(p, prompt)
                data = validate(parse_json(text))
                db.x("INSERT OR REPLACE INTO llm_cache(key, provider, response, created_at) VALUES(?,?,?,?)",
                     key, p["name"], json.dumps(data), iso(utcnow()))
                return data, p["name"]
            except (Invalid, json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
                errors.append(f"{p['name']}: invalid output ({e})")
            except Exception as e:
                errors.append(f"{p['name']}: {str(e)[:200]}")
                if _rate_limited(e):
                    _cooldown[p["name"]] = time.time() + 90
                elif p.get("optional"):
                    _cooldown[p["name"]] = time.time() + 600
                break
    raise LLMError("; ".join(errors))


MOMENTS_VIDEO_PROMPT = """You are a senior short-form video editor. You are watching part of a YouTube {kind}.
Content: {topic}. Target viewers: {audience}.
This part runs from {start} to {end} of the full video ({total} long).

Find the moments with the best chance to go viral as a vertical short for those viewers:
funny moments, crazy or chaotic moments, rage, fails, clutch plays, shocking events, big reactions,
strong stories, hot takes and useful insights.

Rules:
- Each moment is {min_len} to {max_len} seconds long and makes sense without context.
- Start on the action itself (no slow intro), so something happens in the very first second.
- Skip sponsor reads, ads, waiting screens, menus, slurs, and major story spoilers.
- Use absolute timestamps in the full video (H:MM:SS), between {start} and {end}.
- Score honestly: 9-10 only for moments you would bet on. Return at most {limit} moments, or [] if none are strong.
- Write hook and why in American English.{rules}

Return a JSON array only:
[{{"start": "H:MM:SS", "end": "H:MM:SS", "category": "funny|crazy|fail|clutch|reaction|shock|story|insight|hot_take",
  "score": 1-10, "hook": "max 6 words for on-screen text", "why": "one sentence",
  "language": "two-letter code of the language spoken in this moment, e.g. en, hi, es"}}]"""

MOMENTS_TRANSCRIPT_PROMPT = """You are a senior short-form video editor.
Content: {topic}. Target viewers: {audience}.
Below are transcript windows from a {kind} ({total} long). Lines start with absolute timestamps.
Windows marked with a loudness spike had sudden yelling, laughing or noise.

Pick the moments with the best chance to go viral as a vertical short for those viewers:
funny, crazy, rage, fails, clutch plays, shocking events, big reactions, strong stories, hot takes, useful insights.

Rules:
- Each moment is {min_len} to {max_len} seconds long and makes sense without context.
- Start on the action itself, no slow intro. Use absolute timestamps (H:MM:SS) taken from the lines.
- Skip sponsor reads, slurs and major story spoilers. Return at most {limit} moments, or [] if none are strong.
- Write hook and why in American English.{rules}

Return a JSON array only:
[{{"start": "H:MM:SS", "end": "H:MM:SS", "category": "funny|crazy|fail|clutch|reaction|shock|story|insight|hot_take",
  "score": 1-10, "hook": "max 6 words for on-screen text", "why": "one sentence",
  "language": "two-letter code of the language spoken in this moment, e.g. en, hi, es"}}]

Windows:
{windows}"""

METADATA_PROMPT = """Write metadata for a short-form vertical clip (YouTube Shorts, Instagram Reels).
Content: {topic}. Target viewers: {audience}.
Moment type: {category}
Hook: {hook}
What happens: {why}
What is said in the clip: {transcript}{rules}

Return JSON only:
{{"titles": ["3 options, each under 60 characters, curiosity without lying, no emojis"],
  "description": "1 to 2 short sentences",
  "hashtags": ["3 to 5 specific hashtags"]}}
American English only."""


def rules_block(campaign):
    """The campaign's rules plus its guide (reference material), for every AI prompt about this campaign."""
    from . import guide
    rules = list(campaign.get("rules") or []) + guide.lines(campaign)
    if not rules:
        return ""
    return "\n\nCampaign rules and guide from the creator (follow them):\n" + "\n".join(f"- {r}" for r in rules)
