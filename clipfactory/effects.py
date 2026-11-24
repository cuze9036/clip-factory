"""The editor's hands: effects that make a clip look edited, driven by an "edit plan".

An edit plan is a small dict (times in seconds of the raw clip):

    {"cuts":    [[1.2, 1.9]],                      # pauses / filler to remove (jump cuts)
     "hook":    {"text": "WAIT FOR IT", "until": 1.8},   # big text over the first seconds
     "zooms":   [{"t": 3.2, "scale": 1.18, "dur": 0.6}],  # punch zooms
     "flashes": [3.2],                              # white flash
     "shakes":  [{"t": 5.0, "dur": 0.3}],
     "emojis":  [{"t": 2.0, "dur": 0.9, "emoji": "😂"}],
     "emphasis":[4, 9],                             # word indices shown bigger, in the accent colour
     "sfx":     [{"t": 3.2, "kind": "whoosh"}]}

auto_plan() makes one from the words alone (no AI). Later an AI director can hand in its own plan with the
same keys; anything missing or out of range is ignored or clamped, so a bad plan can't break a render.
"""
import json
import re
import subprocess

from . import config

EMPHASIS_WORDS = {"insane", "crazy", "never", "what", "why", "wow", "money", "dead", "literally", "best", "worst",
                  "huge", "million", "billion", "unbelievable", "impossible", "bro", "omg", "no", "yes", "wait",
                  "seriously", "actually", "secret", "biggest", "first", "last", "dangerous", "scared", "love"}
EMOJI_RULES = [
    (r"^(haha+|lol|lmao|funny|hilarious|laugh\w*|joke)$", "😂"),
    (r"^(insane|crazy|wild|unbelievable|impossible|mind)$", "🤯"),
    (r"^(fire|best|goat|amazing|incredible|legend\w*)$", "🔥"),
    (r"^(what|wait|no|really|seriously|whoa|wow|omg)$", "😳"),
    (r"^(money|rich|cash|dollars?|million\w*|billion\w*|paid)$", "💰"),
    (r"^(dead|died|rip|dying|kill\w*)$", "💀"),
    (r"^(love|heart|cute|beautiful)$", "❤️"),
    (r"^(sad|cry\w*|tears)$", "😭"),
]
SFX = {   # generated with FFmpeg once, no downloads
    "whoosh": "anoisesrc=d=0.45:c=pink:a=0.7,highpass=f=500,lowpass=f=7000,afade=t=in:d=0.12,afade=t=out:st=0.18:d=0.27",
    "boom": "sine=f=55:d=0.7,volume=2.2,afade=t=out:d=0.7",
    "pop": "sine=f=880:d=0.09,afade=t=out:d=0.09",
}
YUNET = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/"
         "face_detection_yunet_2023mar.onnx")


def _word(w):
    return re.sub(r"[^a-z]", "", (w or "").lower())


# ---------- the plan ----------

def auto_plan(words, duration, hook="", style="bold_crop", rng=None):
    """A lively default edit from the transcript timing alone."""
    import random
    rng = rng or random.Random(len(words) * 7 + int(duration))
    cfg = config.section("effects")
    plan = {"cuts": [], "zooms": [], "flashes": [], "shakes": [], "emojis": [], "emphasis": [], "sfx": []}
    # jump cuts: long pauses between words (keep a little air), never more than a third of the clip
    gap = cfg.get("cut_pauses_over", 0.7)
    for a, b in zip(words, words[1:]):
        if b["s"] - a["e"] > gap:
            plan["cuts"].append([round(a["e"] + 0.15, 2), round(b["s"] - 0.12, 2)])
    if words and words[0]["s"] > 0.6:
        plan["cuts"].insert(0, [0.0, round(words[0]["s"] - 0.25, 2)])
    if sum(e - s for s, e in plan["cuts"]) > duration / 3:
        plan["cuts"] = []
    # emphasis: strong words, spaced out
    scored = []
    for i, w in enumerate(words):
        raw, low = w["w"], _word(w["w"])
        s = (2 if re.search(r"[!?]", raw) else 0) + (2 if low in EMPHASIS_WORDS else 0) + (1 if len(low) >= 7 else 0) \
            + (1 if raw.isupper() and len(low) > 1 else 0)
        if s:
            scored.append((s, i))
    picked = []
    for s, i in sorted(scored, key=lambda x: (-x[0], x[1])):
        if all(abs(words[i]["s"] - words[j]["s"]) >= 2.5 for j in picked):
            picked.append(i)
        if len(picked) >= cfg.get("max_zooms", 3):
            break
    picked.sort()
    plan["emphasis"] = picked
    for n, i in enumerate(picked):
        t = words[i]["s"]
        plan["zooms"].append({"t": t, "scale": round(rng.uniform(1.12, 1.22), 2), "dur": 0.7})
        plan["sfx"].append({"t": max(0.0, t - 0.08), "kind": "whoosh"})
        if n == 0 and style != "meme_top":
            plan["flashes"].append(t)
        if "!" in words[i]["w"]:
            plan["shakes"].append({"t": t, "dur": 0.3})
    # emojis on matching words (max 3, spaced)
    for i, w in enumerate(words):
        low = _word(w["w"])
        for rx, emo in EMOJI_RULES:
            if re.match(rx, low) and all(abs(w["s"] - e["t"]) > 2.0 for e in plan["emojis"]):
                plan["emojis"].append({"t": w["s"], "dur": 0.9, "emoji": emo})
                break
        if len(plan["emojis"]) >= 3:
            break
    if hook and style in ("bold_crop", "word_pop", "split"):
        plan["hook"] = {"text": hook, "until": cfg.get("hook_seconds", 1.8)}
    return plan


def clean(plan, duration):
    """Clamp and drop anything unusable, so an AI's plan can't break the render."""
    p = plan or {}
    num = lambda v, lo, hi, d=0.0: max(lo, min(hi, float(v))) if isinstance(v, (int, float)) else d  # noqa: E731
    out = {"cuts": [], "zooms": [], "flashes": [], "shakes": [], "emojis": [], "emphasis": [], "sfx": []}
    for c in p.get("cuts") or []:
        if isinstance(c, (list, tuple)) and len(c) == 2 and 0 <= c[0] < c[1] <= duration and c[1] - c[0] >= 0.15:
            out["cuts"].append([float(c[0]), float(c[1])])
    if sum(e - s for s, e in out["cuts"]) > duration * 0.5:
        out["cuts"] = []
    for z in (p.get("zooms") or [])[:6]:
        if isinstance(z, dict) and 0 <= z.get("t", -1) < duration:
            out["zooms"].append({"t": float(z["t"]), "scale": num(z.get("scale", 1.15), 1.03, 1.4, 1.15),
                                 "dur": num(z.get("dur", 0.6), 0.2, 2.0, 0.6)})
    out["flashes"] = [float(t) for t in (p.get("flashes") or [])[:3] if isinstance(t, (int, float)) and 0 <= t < duration]
    for s in (p.get("shakes") or [])[:3]:
        if isinstance(s, dict) and 0 <= s.get("t", -1) < duration:
            out["shakes"].append({"t": float(s["t"]), "dur": num(s.get("dur", 0.3), 0.1, 1.0, 0.3)})
    for e in (p.get("emojis") or [])[:4]:
        if isinstance(e, dict) and 0 <= e.get("t", -1) < duration and e.get("emoji"):
            out["emojis"].append({"t": float(e["t"]), "dur": num(e.get("dur", 0.9), 0.3, 3.0, 0.9),
                                  "emoji": str(e["emoji"])[:8]})
    out["emphasis"] = [int(i) for i in (p.get("emphasis") or [])[:6] if isinstance(i, int) and i >= 0]
    for s in (p.get("sfx") or [])[:6]:
        if isinstance(s, dict) and s.get("kind") in SFX and 0 <= s.get("t", -1) < duration:
            out["sfx"].append({"t": float(s["t"]), "kind": s["kind"]})
    h = p.get("hook")
    if isinstance(h, dict) and str(h.get("text") or "").strip():
        out["hook"] = {"text": str(h["text"])[:60], "until": num(h.get("until", 1.8), 0.8, 4.0, 1.8)}
    return out


# ---------- jump cuts: a new timeline ----------

def keep_spans(cuts, duration):
    spans, cur = [], 0.0
    for s, e in sorted(cuts):
        if s > cur:
            spans.append([cur, s])
        cur = max(cur, e)
    if cur < duration:
        spans.append([cur, duration])
    return [sp for sp in spans if sp[1] - sp[0] > 0.05]


def remap(t, spans):
    """Raw-clip time -> time in the cut clip (None if that moment was cut)."""
    acc = 0.0
    for s, e in spans:
        if s <= t <= e:
            return round(acc + (t - s), 3)
        acc += e - s
    return None


def apply_cuts(words, plan, spans):
    """Words and plan moved onto the cut timeline."""
    new_words, index = [], {}
    for i, w in enumerate(words):
        s, e = remap(w["s"], spans), remap(w["e"], spans)
        if s is None:
            continue
        index[i] = len(new_words)
        new_words.append({**w, "s": s, "e": e if e is not None else s + 0.25})
    p = {k: [] for k in ("zooms", "flashes", "shakes", "emojis", "sfx")}
    for k in ("zooms", "shakes", "emojis", "sfx"):
        for item in plan.get(k, []):
            t = remap(item["t"], spans)
            if t is not None:
                p[k].append({**item, "t": t})
    p["flashes"] = [t for t in (remap(t, spans) for t in plan.get("flashes", [])) if t is not None]
    p["emphasis"] = [index[i] for i in plan.get("emphasis", []) if i in index]
    p["cuts"] = []
    if plan.get("hook"):
        p["hook"] = plan["hook"]
    return new_words, p


def cut_video(src, spans, out):
    """Write the clip with the cut spans removed (picture and sound stay in sync)."""
    sel = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in spans)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                    "-vf", f"select='{sel}',setpts=N/FRAME_RATE/TB",
                    "-af", f"aselect='{sel}',asetpts=N/SR/TB",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-c:a", "aac", "-b:a", "192k", str(out)],
                   check=True, capture_output=True)
    return out


# ---------- face tracking (keeps the speaker in frame) ----------

def _yunet():
    import requests
    p = config.path("assets/models/face_detection_yunet_2023mar.onnx")
    if not p.exists() or p.stat().st_size < 100_000:
        p.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(YUNET, timeout=60)
        if r.status_code == 200 and len(r.content) > 100_000:
            p.write_bytes(r.content)
    return p if p.exists() else None


def find_facecam(path, samples=12):
    """A streamer's webcam in the footage: (x, y, w, h) as 0..1 of the frame, else None.
    It is a small face off to one side that stays in the same place in most frames (faces in the game move
    around). The box is the steady frame drawn around the webcam when there is one, else a box around the face."""
    try:
        import cv2
        import numpy as np
        model = _yunet()
        if not model or not hasattr(cv2, "FaceDetectorYN"):
            return None
        cap = cv2.VideoCapture(str(path))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        det, hits, acc, n = None, [], None, 0
        for k in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / samples))
            ok, f = cap.read()
            if not ok:
                continue
            f = cv2.resize(f, (960, int(f.shape[0] * 960 / f.shape[1])))
            h, w = f.shape[:2]
            if h >= w:   # tall footage has no webcam box to split off
                cap.release()
                return None
            n += 1
            if det is None:
                det = cv2.FaceDetectorYN.create(str(model), "", (w, h), 0.7)
            _, faces = det.detect(f)
            e = cv2.Canny(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), 60, 160) > 0
            acc = e.astype(np.float32) if acc is None else acc + e
            for x, y, fw, fh in ([fc[:4] for fc in faces] if faces is not None else []):
                cx, cy = (x + fw / 2) / w, (y + fh / 2) / h
                if fw / w < 0.2 and (cx < 0.33 or cx > 0.67):
                    hits.append((cx, cy, fw / w, fh / h))
        cap.release()
        groups = [[q for q in hits if abs(q[0] - p[0]) < 0.04 and abs(q[1] - p[1]) < 0.04] for p in hits]
        best = max(groups, key=len) if groups else []
        if n == 0 or len(best) < n * 0.6:
            return None
        cx, cy, fw, fh = (float(v) for v in np.median(np.array(best), axis=0))
        H, W = acc.shape
        steady = cv2.dilate(((acc / n) > 0.7).astype(np.uint8) * 255, np.ones((3, 3), np.uint8))
        cnts, _ = cv2.findContours(steady, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        px, py = cx * W, cy * H
        boxes = []
        for c in cnts:
            x, y, bw, bh = cv2.boundingRect(c)
            if x < px < x + bw and y < py < y + bh and bw > fw * W * 1.5 and bh > fh * H * 1.3                     and bw * bh < W * H * 0.3:
                boxes.append((bw * bh, x, y, bw, bh))
        if boxes:
            _, x, y, bw, bh = min(boxes)
        else:
            x, y = max(0.0, px - 1.6 * fw * W), max(0.0, py - 1.2 * fh * H)
            bw, bh = min(W, px + 1.6 * fw * W) - x, min(H, py + 1.2 * fh * H) - y
        return [round(x / W, 3), round(y / H, 3), round(bw / W, 3), round(bh / H, 3)]
    except Exception:
        return None


def face_track(path, every=0.5):
    """[(t, x as 0..1)] where the biggest face is, smoothed; None if there are no steady faces or no OpenCV."""
    try:
        import cv2
        model = _yunet()
        if not model or not hasattr(cv2, "FaceDetectorYN"):
            return None
        cap = cv2.VideoCapture(str(path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        det, pts, i = None, [], 0
        step = max(1, int(fps * every))
        while i < total:
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, frame = cap.read()
            if not ok:
                break
            h, w = frame.shape[:2]
            if det is None:
                det = cv2.FaceDetectorYN.create(str(model), "", (w, h), 0.7)
            _, faces = det.detect(frame)
            if faces is not None and len(faces):
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])[:4]
                pts.append((i / fps, float((x + fw / 2) / w)))
            i += step
        cap.release()
    except Exception:
        return None
    if len(pts) < 3:
        return None
    smooth = []
    for k, (t, _) in enumerate(pts):
        window = [x for _, x in pts[max(0, k - 2):k + 3]]
        smooth.append((round(t, 2), round(sum(window) / len(window), 3)))
    return smooth


def x_expr(points, crop_w_expr="ih*9/16"):
    """FFmpeg crop x(t) that follows the face points (linear between them), clamped to the frame."""
    if not points:
        return f"(iw-{crop_w_expr})/2"
    def at(cx):
        return f"({cx}*iw-{crop_w_expr}/2)"
    expr = at(points[-1][1])
    for (t0, x0), (t1, x1) in reversed(list(zip(points, points[1:]))):
        lerp = f"({x0}+({x1}-{x0})*(t-{t0})/{max(0.01, t1 - t0)})"
        expr = f"if(lt(t,{t1}),{at(lerp)},{expr})"
    expr = f"if(lt(t,{points[0][0]}),{at(points[0][1])},{expr})"
    return f"max(0,min(iw-{crop_w_expr},{expr}))"


# ---------- text already burned into the video ----------

def has_burned_captions(path, samples=12):
    """True when the footage already shows captions: white text with a dark outline or a soft drop shadow, centred
    and spread across the lower part of the frame (the usual subtitle look), in enough frames. Captions come and go
    with speech, so a third of the frames is enough; game HUDs and white clothes sit off-centre or are too narrow.
    Then we don't add our own captions on top."""
    try:
        import cv2
        import numpy as np
        cap = cv2.VideoCapture(str(path))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        hits = seen = 0
        kernel = np.ones((5, 5), np.uint8)
        for k in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / samples))
            ok, frame = cap.read()
            if not ok:
                continue
            seen += 1
            h = frame.shape[0]
            band = frame[int(h * 0.45):int(h * 0.95)]
            white = np.all(band > 215, axis=2).astype(np.uint8)
            gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
            shadow = (cv2.GaussianBlur(gray, (31, 31), 0).astype(int) - gray) > 40   # darker than its surroundings
            edge = cv2.dilate((np.all(band < 45, axis=2) | shadow).astype(np.uint8), kernel)
            text = white & edge
            if text.sum() / text.size <= 0.0015:
                continue
            xs = np.nonzero(text)[1] / text.shape[1]
            if abs(xs.mean() - 0.5) < 0.12 and np.percentile(xs, 95) - np.percentile(xs, 5) > 0.15:
                hits += 1
        cap.release()
        return seen > 0 and hits / seen >= 0.3
    except Exception:
        return False


# ---------- motion: zooms, shakes, flashes ----------

def zoom_expr(plan, duration, push=0.06):
    z = f"1+{push}*t/{max(duration, 1):.2f}"
    for zm in plan.get("zooms", []):
        a, t0, d = zm["scale"] - 1, zm["t"], zm["dur"]
        z += f"+{a:.3f}*min(1,(t-{t0:.2f})/0.07)*between(t,{t0:.2f},{t0 + d:.2f})"
    return z


def motion(width, height, plan, duration):
    """Filter chain for a width x height picture: slow push-in, punch zooms, shakes."""
    z = zoom_expr(plan, duration)
    shake = "+".join(f"between(t,{s['t']:.2f},{s['t'] + s['dur']:.2f})" for s in plan.get("shakes", [])) or "0"
    return (f"scale=w='trunc({width}*({z})/2)*2':h='trunc({height}*({z})/2)*2':eval=frame,"
            f"crop={width}:{height}:x='(iw-{width})/2+14*sin(t*57)*({shake})':"
            f"y='(ih-{height})/2+10*cos(t*43)*({shake})'")


def flashes(plan):
    """drawbox steps (append to a chain) for white flashes."""
    return "".join(f",drawbox=x=0:y=0:w=iw:h=ih:color=white@0.8:t=fill:enable='between(t,{t:.2f},{t + 0.09:.2f})'"
                   for t in plan.get("flashes", []))


# ---------- emojis and sound effects ----------

def emoji_png(emoji):
    """A Twemoji PNG for this emoji (downloaded once), or None."""
    import requests
    code = "-".join(f"{ord(c):x}" for c in emoji if ord(c) != 0xFE0F)
    p = config.path(f"assets/emoji/{code}.png")
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            r = requests.get(f"https://raw.githubusercontent.com/jdecked/twemoji/main/assets/72x72/{code}.png", timeout=30)
            if r.status_code == 200:
                p.write_bytes(r.content)
        except Exception:
            return None
    return p if p.exists() else None


def sfx_file(kind):
    p = config.path(f"assets/sfx/{kind}.wav")
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", SFX[kind], "-ar", "48000", "-ac", "2", str(p)],
                       check=True, capture_output=True)
    return p


def dumps(plan):
    return json.dumps(plan, ensure_ascii=False)
