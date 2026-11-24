"""Where our text goes: never on text that is already in the picture.

Before a clip is rendered, a few frames of the styled video (without our captions) are looked at. Any text already
there (the footage's subtitles, a title, a game HUD, a sign) marks its horizontal band as taken, and our captions,
@handle and hook text are put in the free bands nearest to where each style normally has them.
"""
import subprocess

import numpy as np

H = 1920
BANDS = 40                      # 48 px each on the 1080x1920 canvas
TAKEN = 0.25                    # text in this share of the sampled frames -> the band is taken
HALF = {"cap": 100, "credit": 32, "hook": 95, "emoji": 80}   # half-height of each of our elements, in px

CAP_SPOTS = [1240, 1400, 1080, 1540, 940, 1660, 800]
CREDIT_SPOTS = [1390, 200, 1790, 300, 1680, 120]
HOOK_SPOTS = [330, 480, 600, 220, 720]


def sample_frames(path, vf, duration, n=10, width=540):
    """n frames of the styled video (the style's filter graph, no captions), small, as BGR arrays."""
    step = max(duration / n, 0.2)
    graph = (f"{vf};[v0]select='isnan(prev_selected_t)+gte(t-prev_selected_t,{step:.3f})',"
             f"scale={width}:-2,format=bgr24[s]")
    h = width * 16 // 9 // 2 * 2
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-filter_complex", graph, "-map", "[s]",
                        "-frames:v", str(n), "-fps_mode", "vfr", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=180)
    if r.returncode != 0 or not r.stdout:
        return []
    size = width * h * 3
    return [np.frombuffer(r.stdout[i:i + size], np.uint8).reshape(h, width, 3)
            for i in range(0, len(r.stdout) - size + 1, size)]


def text_boxes(frame):
    """Boxes (x, y, w, h) that look like a line of text: sharp strokes side by side, any colour."""
    import cv2
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = g.shape
    grad = cv2.morphologyEx(g, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    _, bw = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    bw = cv2.bitwise_and(bw, (grad > 60).astype(np.uint8) * 255)   # only strong, sharp edges
    joined = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (max(9, w // 40), 3)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(joined, 8)
    boxes = []
    for i in range(1, n):
        x, y, bw_, bh, _ = stats[i]
        if bh < h * 0.012 or bh > h * 0.09 or bw_ < bh * 2 or bw_ < w * 0.06:
            continue
        roi = bw[y:y + bh, x:x + bw_]
        fill = (roi > 0).mean()
        if fill < 0.25 or fill > 0.85:
            continue
        mid = roi[bh // 2] > 0   # letters: many strokes across the middle of the line
        if np.count_nonzero(mid[1:] != mid[:-1]) / max(1, bw_ / bh) < 1.2:
            continue
        boxes.append((int(x), int(y), int(bw_), int(bh)))
    return boxes


def text_rows(frames):
    """For each of the BANDS horizontal bands: the share of frames with text in it."""
    rows = np.zeros(BANDS)
    for f in frames:
        hit = np.zeros(BANDS, bool)
        fh = f.shape[0]
        for _, y, _, bh in text_boxes(f):
            hit[int(y / fh * BANDS):min(BANDS, int((y + bh) / fh * BANDS) + 1)] = True
        rows += hit
    return rows / max(1, len(frames))


def busy(rows, y, half):
    """How much text sits where an element centred at y (half-height `half`) would go: 0 = free."""
    a, b = max(0, int((y - half) / H * BANDS)), min(BANDS, int((y + half) / H * BANDS) + 1)
    return float(rows[a:b].max()) if b > a else 0.0


def _pick(rows, spots, half, avoid=()):
    """The first spot (they're in order of preference) that is free and clear of our other elements; else the
    least busy one."""
    ok = [y for y in spots if all(abs(y - o) > half + oh + 10 for o, oh in avoid)]
    ok = ok or list(spots)
    for y in ok:
        if busy(rows, y, half) < TAKEN:
            return y
    return min(ok, key=lambda y: busy(rows, y, half))


def place(look, rows, captions=True, hook=False):
    """Positions for our elements: {"cap_y", "credit_y", "hook_y"} plus the text that was found, for the log."""
    default_cap, default_credit = look["cap_y"], look["credit_y"]
    out = {}
    taken = []
    if captions:
        spots = [default_cap] + [y for y in CAP_SPOTS if y != default_cap]
        out["cap_y"] = _pick(rows, spots, HALF["cap"])
        taken.append((out["cap_y"], HALF["cap"]))
        taken.append((out["cap_y"] - 240, HALF["emoji"]))   # emojis sit just above the captions
    if hook:
        out["hook_y"] = _pick(rows, HOOK_SPOTS, HALF["hook"], taken)
        taken.append((out["hook_y"], HALF["hook"]))
    first = 200 if not captions else default_credit   # the footage's own captions sit low: the handle goes up
    spots = [first] + [y for y in CREDIT_SPOTS if y != first]
    out["credit_y"] = _pick(rows, spots, HALF["credit"], taken)
    out["text_bands"] = [round(i * H / BANDS) for i, v in enumerate(rows) if v >= TAKEN]
    return out
