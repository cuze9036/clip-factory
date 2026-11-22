"""Edit styles from the viral-clip research (docs: 51 top Shorts/Reels measured on a 1080x1920 canvas).

Every clip gets one style, picked to fit the content, with some variety so the weekly review can see which style
earns the most views. Styles:

  bold_crop    full-frame crop, slow push-in, 3-word ALL CAPS captions, spoken word in colour, short @credit
  word_pop     full-frame crop, one big word at a time that pops in (fast talkers, reactions)
  blur_box     blurred copy of the video behind it, video band in the middle, white title box on top
  black_title  black canvas, square video, sentence-case title on top, captions inside the video
  meme_top     white band on top with a black comic ALL CAPS title, video fills the rest
  split        streamer facecam on top, content below, captions on the split line (needs a facecam box)

Safe zone: nothing important below y 1450 or right of x 940 (platform buttons cover it).
"""
import random
import re

from . import config

W, H = 1080, 1920

FONTS = {   # file name -> URL (downloaded once into assets/fonts)
    "Montserrat-Black.ttf": "https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-Black.ttf",
    "Montserrat-ExtraBold.ttf": "https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-ExtraBold.ttf",
    "Montserrat-ExtraBoldItalic.ttf": "https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-ExtraBoldItalic.ttf",
    "Montserrat-SemiBold.ttf": "https://raw.githubusercontent.com/JulietaUla/Montserrat/master/fonts/ttf/Montserrat-SemiBold.ttf",
    "Anton-Regular.ttf": "https://raw.githubusercontent.com/google/fonts/main/ofl/anton/Anton-Regular.ttf",
    "Bangers-Regular.ttf": "https://raw.githubusercontent.com/google/fonts/main/ofl/bangers/Bangers-Regular.ttf",
}

TALK = re.compile(r"podcast|interview|talk|comedy|stand.?up|money|business|finance|advice|motivat|speech|debate", re.I)
SHOW = re.compile(r"concert|music|song|stage|fail|prank|funny|meme|unexpected", re.I)
WIDE = re.compile(r"gam|stream|sport|match|goal|nba|nfl|football|soccer|movie|film|tv|scene|race|fight", re.I)

ACCENTS = {"yellow": "#FFD400", "green": "#3CFF5A", "red": "#FF2D2D"}


def ensure_fonts(folder=None):
    """Download the style fonts once; missing fonts just fall back to Montserrat/Arial."""
    import requests
    folder = folder or config.path(config.section("render").get("fonts_dir", "assets/fonts"))
    folder.mkdir(parents=True, exist_ok=True)
    for name, url in FONTS.items():
        f = folder / name
        if f.exists() and f.stat().st_size > 10000:
            continue
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 200 and len(r.content) > 10000:
                f.write_bytes(r.content)
        except Exception:
            pass
    return folder


def ass_colour(hex_rgb, alpha=0):
    """#RRGGBB -> ASS &HAABBGGRR&."""
    h = hex_rgb.lstrip("#").upper()
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}&"


def accent_for(topic, category=""):
    text = f"{topic} {category}"
    if re.search(r"comedy|stand.?up|funny|laugh", text, re.I):
        return ACCENTS["green"]
    if re.search(r"sport|fight|drama|fail|crash|shock", text, re.I):
        return ACCENTS["red"]
    return ACCENTS["yellow"]


def choose(topic, category="", facecam=False, avoid=None, rng=random):
    """A style that fits the content, with some variety (weights from the research)."""
    if facecam:
        return "split"
    options = {"bold_crop": 5, "meme_top": 2, "blur_box": 2, "black_title": 1}
    for text in (topic or "", category or ""):   # the campaign's topic decides first, the moment type second
        if TALK.search(text):
            options = {"bold_crop": 6, "word_pop": 2, "black_title": 2}
        elif WIDE.search(text):
            options = {"blur_box": 5, "black_title": 3, "word_pop": 2}
        elif SHOW.search(text):
            options = {"meme_top": 5, "bold_crop": 4, "word_pop": 1}
        else:
            continue
        break
    fixed = (config.section("render").get("style") or "auto")
    if fixed != "auto" and fixed in STYLE_NAMES:
        return fixed
    options = {k: v for k, v in options.items() if k != avoid} or options
    names, weights = zip(*options.items())
    return rng.choices(names, weights)[0]


STYLE_NAMES = ("bold_crop", "word_pop", "blur_box", "black_title", "meme_top", "split")


# ---------- text helpers ----------

EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]+")


def _clean(text):
    return re.sub(r"\s+", " ", EMOJI.sub("", re.sub(r"[{}\\]", "", text or ""))).strip()


def short_credit(credit):
    """Only the first @handle goes on screen ("@mby"); the full credit line stays in the post caption."""
    m = re.search(r"@[\w.]{2,30}", credit or "")
    return m.group(0) if m else ""


def on_screen_title(hook, limit=48):
    t = _clean(hook).rstrip(".")
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "…"
    return t


def _t(sec):
    sec = max(0.0, sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _groups(words, size):
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= size or w["s"] - cur[-1]["e"] > 0.6):
            groups.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?]$", w["w"]):
            groups.append(cur)
            cur = []
    return groups + ([cur] if cur else [])


# ---------- per-style layout ----------
# caption: y centre, words per line, font, size, outline, pop (scale %), case
LOOK = {
    "bold_crop":   {"cap_y": 1240, "per": 3, "font": "Montserrat Black", "size": 84, "outline": 8, "pop": 80,
                    "credit_y": 1390},
    "word_pop":    {"cap_y": 1060, "per": 1, "font": "Anton", "size": 150, "outline": 10, "pop": 70,
                    "credit_y": 1390},
    "blur_box":    {"cap_y": 1420, "per": 1, "font": "Montserrat Black", "size": 86, "outline": 8, "pop": 75,
                    "title_y": 300, "credit_y": 560},
    "black_title": {"cap_y": 1300, "per": 3, "font": "Montserrat Black", "size": 72, "outline": 7, "pop": 85,
                    "title_y": 290, "credit_y": 1440},
    "meme_top":    {"cap_y": 1300, "per": 3, "font": "Montserrat Black", "size": 72, "outline": 7, "pop": 85,
                    "title_y": 262, "credit_y": 1395},
    "split":       {"cap_y": 960, "per": 1, "font": "Montserrat Black", "size": 84, "outline": 8, "pop": 75,
                    "credit_y": 1390},
}


def build_ass(style, words, hook, duration, credit="", accent="#FFD400"):
    look = LOOK[style]
    acc = ass_colour(accent)
    white = "&H00FFFFFF&"
    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Cap,{look['font']},{look['size']},{white},{white},&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,"
        f"{look['outline']},3,5,80,140,0,1",
        "Style: Credit,Montserrat ExtraBold,38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,1,0,0,100,100,0,0,1,"
        "3,2,5,80,140,0,1",
        "Style: TitleBox,Montserrat Black,66,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,0,0,3,"
        "16,0,5,90,90,0,1",
        "Style: TitleDark,Montserrat ExtraBold,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,"
        "0,2,5,70,70,0,1",
        "Style: TitleMeme,Bangers,124,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,0,0,0,0,100,100,3,0,1,0,0,5,70,70,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    ev = []
    title = on_screen_title(hook)
    if title and "title_y" in look:
        y = look["title_y"]
        if style == "blur_box":
            ev.append(f"Dialogue: 2,{_t(0)},{_t(duration)},TitleBox,,0,0,0,,{{\\an5\\pos(540,{y})}}{title}")
        elif style == "black_title":
            ev.append(f"Dialogue: 2,{_t(0)},{_t(duration)},TitleDark,,0,0,0,,{{\\an5\\pos(540,{y})}}{title}")
        elif style == "meme_top":
            ev.append(f"Dialogue: 2,{_t(0)},{_t(duration)},TitleMeme,,0,0,0,,{{\\an5\\pos(540,{y})}}{title.upper()}")
    handle = short_credit(credit)
    if handle:
        cy = look["credit_y"]
        colour = acc if style in ("bold_crop", "word_pop", "split") else "&H00FFFFFF&"
        ev.append(f"Dialogue: 1,{_t(0)},{_t(duration)},Credit,,0,0,0,,{{\\an5\\pos(540,{cy})\\c{colour}}}{handle}")
    y, pop = look["cap_y"], look["pop"]
    groups = _groups(words, look["per"])
    for gi, g in enumerate(groups):
        nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else duration
        g_end = min(g[-1]["e"] + 0.3, nxt)
        for wi, w in enumerate(g):
            start = w["s"]
            end = g[wi + 1]["s"] if wi + 1 < len(g) else g_end
            if end <= start:
                continue
            words_txt = [_clean(x["w"]).upper() for x in g]
            if not any(words_txt):
                continue
            if look["per"] == 1:   # one word: long words in the accent colour, short ones white
                txt = words_txt[0]
                colour = acc if len(re.sub(r"[^A-Z]", "", txt)) >= 6 else white
                text = f"{{\\c{colour}}}{txt}"
            else:                   # the spoken word in the accent colour
                text = " ".join(f"{{\\c{acc}}}{t}{{\\c{white}}}" if j == wi else t for j, t in enumerate(words_txt))
            anim = (f"\\fscx{pop}\\fscy{pop}\\t(0,90,\\fscx108\\fscy108)\\t(90,150,\\fscx100\\fscy100)"
                    if wi == 0 else "")
            ev.append(f"Dialogue: 0,{_t(start)},{_t(end)},Cap,,0,0,0,,{{\\an5\\pos(540,{y}){anim}}}{text}")
    return "\n".join(head + ev) + "\n"


def video_filter(style, src, facecam=None, duration=10.0):
    """FFmpeg filter graph from [0:v] to [v0] for a style."""
    fps = min(60, max(24, round(src.get("fps") or 30)))
    grade = "eq=contrast=1.06:saturation=1.18,unsharp=5:5:0.5"
    d = max(duration, 1.0)
    if style in ("bold_crop", "word_pop"):
        # fill the frame, then a slow push-in (0% -> 7%) so the clip never looks static
        return (f"[0:v]scale=-2:{H},crop={W}:{H},{grade},"
                f"scale=w='trunc({W}*(1+0.07*t/{d:.2f})/2)*2':h='trunc({H}*(1+0.07*t/{d:.2f})/2)*2':eval=frame,"
                f"crop={W}:{H},setsar=1[v0]")
    if style == "blur_box":
        return (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"boxblur=40:2,eq=brightness=-0.22:saturation=1.1[bg];"
                f"[b]scale=-2:740,crop={W}:740,{grade}[fg];[bg][fg]overlay=0:600,setsar=1[v0]")
    if style == "black_title":
        return (f"color=c=black:s={W}x{H}:r={fps}[bg];[0:v]scale=-2:1080,crop=1080:1080,{grade}[fg];"
                f"[bg][fg]overlay=0:420:shortest=1,setsar=1[v0]")
    if style == "meme_top":
        return (f"color=c=white:s={W}x{H}:r={fps}[bg];[0:v]scale={W}:1400:force_original_aspect_ratio=increase,"
                f"crop={W}:1400,{grade}[fg];[bg][fg]overlay=0:520:shortest=1,setsar=1[v0]")
    if style == "split" and facecam:
        fx, fy, fw, fh = facecam
        return (f"[0:v]split[a][b];[a]crop=iw*{fw}:ih*{fh}:iw*{fx}:ih*{fy},scale={W}:960:force_original_aspect_ratio="
                f"increase,crop={W}:960,{grade}[face];[b]scale=-2:960,crop={W}:960,{grade}[game];"
                f"[face][game]vstack,setsar=1[v0]")
    return video_filter("bold_crop", src, None, duration)
