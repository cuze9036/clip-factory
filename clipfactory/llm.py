"""LLM router: tries each configured provider in order, validates JSON, caches answers, enforces English."""
import datetime as dt
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
    if video and video.get("image"):   # a screenshot: Gemini reads the picture
        import mimetypes
        with open(video["image"], "rb") as f:
            parts.append(types.Part.from_bytes(data=f.read(),
                                               mime_type=mimetypes.guess_type(video["image"])[0] or "image/png"))
    elif video:
        meta = {"start_offset": f"{int(video['start'])}s", "end_offset": f"{int(video['end'])}s"}
        if video.get("fps"):
            meta["fps"] = video["fps"]
        if video.get("path"):   # a local file (Dropbox, Drive, folder): upload it so Gemini can watch it
            up = upload_video(client, video["path"])
            fd = types.FileData(file_uri=up["uri"], mime_type=up["mime"])
        else:
            fd = types.FileData(file_uri=video["url"])
        parts.append(types.Part(file_data=fd, video_metadata=types.VideoMetadata(**meta)))
    parts.append(types.Part(text=prompt))
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json", temperature=0.4,
        media_resolution=(None if not video or video.get("image") else types.MediaResolution.MEDIA_RESOLUTION_MEDIUM if video.get("detail")
                          else types.MediaResolution.MEDIA_RESOLUTION_LOW),   # "detail": small text must be readable
    )
    r = client.models.generate_content(model=p["model"], contents=types.Content(role="user", parts=parts), config=cfg)
    return r.text


def upload_video(client, path):
    """Upload a local video to Gemini once (kept ~2 days by Google) and wait until it can be watched."""
    import os
    st = os.stat(path)
    key = f"gemini_file:{path}:{st.st_size}:{st.st_mtime_ns}"
    known = db.kv_get(key)
    if known and utcnow().timestamp() - known["at"] < 40 * 3600:
        return known
    f = client.files.upload(file=str(path))
    waited = 0
    while str(getattr(f.state, "name", f.state)) == "PROCESSING" and waited < 900:
        time.sleep(5)
        waited += 5
        f = client.files.get(name=f.name)
    if str(getattr(f.state, "name", f.state)) != "ACTIVE":
        raise RuntimeError(f"Gemini couldn't process the video ({getattr(f.state, 'name', f.state)})")
    out = {"uri": f.uri, "mime": f.mime_type or "video/mp4", "at": utcnow().timestamp()}
    db.kv_set(key, out)
    return out


PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}


class BudgetExceeded(Exception):
    pass


def claude_spent_today():
    return db.kv_get("claude_usd:" + utcnow().date().isoformat()) or 0.0


def _claude(p, prompt):
    """Claude (Anthropic API) for the thinking work. Stops for the day at daily_budget_usd."""
    import anthropic
    budget = float(p.get("daily_budget_usd", 0.5))
    if claude_spent_today() >= budget:
        raise BudgetExceeded(f"Claude's daily budget (${budget:.2f}) is used up; free AI takes over until tomorrow")
    client = _clients.get(p["name"]) or anthropic.Anthropic(api_key=p["api_key"], timeout=600)
    _clients[p["name"]] = client
    kwargs = dict(model=p["model"], max_tokens=16000, system="Reply with valid JSON only, in American English.",
                  messages=[{"role": "user", "content": prompt}],
                  output_config={"effort": p.get("effort", "medium")})
    try:   # on a safety decline, Anthropic re-runs it on a fallback model inside the same call
        r = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except TypeError:
        r = client.messages.create(**kwargs)
    pin, pout = PRICES.get(p["model"], (4.0, 20.0))
    u = r.usage
    cost = ((u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)) * pin / 1e6 \
        + (getattr(u, "cache_read_input_tokens", 0) or 0) * pin * 0.1 / 1e6 + (u.output_tokens or 0) * pout / 1e6
    db.kv_set("claude_usd:" + utcnow().date().isoformat(), round(claude_spent_today() + cost, 4))
    if r.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request")
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")


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


def _count(provider, task, ok):
    """Today's answers and failures per AI provider and task, for !ai."""
    key = "llm_usage:" + utcnow().date().isoformat()
    u = db.kv_get(key) or {}
    row = u.setdefault(provider, {"ok": 0, "fail": 0, "tasks": {}})
    row["ok" if ok else "fail"] += 1
    if ok:
        row["tasks"][task] = row["tasks"].get(task, 0) + 1
    db.kv_set(key, u)


def usage(days=1):
    """Plain-English usage of each AI over the last `days` days."""
    total = {}
    for i in range(days):
        day = (utcnow() - dt.timedelta(days=i)).date().isoformat()
        for name, r in (db.kv_get("llm_usage:" + day) or {}).items():
            t = total.setdefault(name, {"ok": 0, "fail": 0, "tasks": {}})
            t["ok"] += r["ok"]
            t["fail"] += r["fail"]
            for k, n in r["tasks"].items():
                t["tasks"][k] = t["tasks"].get(k, 0) + n
    if not total:
        return "No AI answers yet in this period."
    lines = [f"**AI use, last {days} day(s)**"]
    for name, t in sorted(total.items(), key=lambda x: -x[1]["ok"]):
        top = ", ".join(f"{k} {n}" for k, n in sorted(t["tasks"].items(), key=lambda x: -x[1])[:5])
        lines.append(f"• **{name}**: {t['ok']} answers, {t['fail']} failed" + (f" · {top}" if top else ""))
    order = config.section("llm").get("text_order") or []
    lines.append("Text work tries " + " → ".join(order) + "; watching videos uses Gemini.")
    if any(p["type"] == "anthropic" for p in providers()):
        budget = next(float(p.get("daily_budget_usd", 0.5)) for p in providers() if p["type"] == "anthropic")
        lines.append(f"Claude today: ${claude_spent_today():.2f} of ${budget:.2f} (thinking tasks only).")
    return "\n".join(lines)


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
    order = config.section("llm").get("text_order") or []
    if not video and order:   # text work: the free models first, Gemini as the fallback
        tried.sort(key=lambda p: order.index(p["name"]) if p["name"] in order else len(order))
    # Claude (when its key is set) only does the thinking tasks, first; everything else stays on the free AI.
    thinking = set(config.section("llm").get("claude_tasks") or [])
    claude = [p for p in tried if p["type"] == "anthropic"]
    tried = [p for p in tried if p["type"] != "anthropic"]
    if task in thinking and not video:
        tried = claude + tried
    if all(p.get("optional") for p in tried):
        errors.append("no LLM provider configured (set GEMINI_API_KEY or FREE_LLM_* in .env)")
    for p in tried:
        for attempt in range(2):
            try:
                text = (_gemini(p, prompt, video) if p["type"] == "gemini" else
                        _claude(p, prompt) if p["type"] == "anthropic" else _openai(p, prompt))
                data = validate(parse_json(text))
                db.x("INSERT OR REPLACE INTO llm_cache(key, provider, response, created_at) VALUES(?,?,?,?)",
                     key, p["name"], json.dumps(data), iso(utcnow()))
                _count(p["name"], task, True)
                return data, p["name"]
            except (Invalid, json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
                errors.append(f"{p['name']}: invalid output ({e})")
                _count(p["name"], task, False)
            except BudgetExceeded as e:
                errors.append(f"{p['name']}: {e}")
                break
            except Exception as e:
                errors.append(f"{p['name']}: {str(e)[:200]}")
                _count(p["name"], task, False)
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
    from . import guide, insights
    rules = list(campaign.get("rules") or []) + guide.lines(campaign)
    block = ("\n\nCampaign rules and guide from the creator (follow them):\n" + "\n".join(f"- {r}" for r in rules)
             if rules else "")
    from . import study
    # what the weekly review and the daily study learned from our own views
    return block + insights.tips_block() + study.block()
