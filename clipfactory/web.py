"""Local dashboard: campaign lanes, moments the AI found, clip review, posts and submissions."""
import json

from flask import Flask, abort, redirect, render_template, request, send_from_directory, url_for

from . import campaigns, config, db, review, watcher
from .util import fmt_ts, iso, own_time, parse_iso, utcnow

app = Flask(__name__)


@app.template_filter("et")
def _et(s):
    if not s:
        return "never"
    return own_time(parse_iso(s))   # the owner's time (India); posts are still planned in US Eastern


@app.template_filter("ago")
def _ago(s):
    if not s:
        return "never"
    mins = int((utcnow() - parse_iso(s)).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 1440:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} d ago"


@app.template_filter("ts")
def _ts(s):
    return fmt_ts(s)


@app.template_filter("num")
def _num(n):
    n = n or 0
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}K" if n >= 1e4 else f"{n:,}"


@app.template_filter("fromjson")
def _fromjson(s):
    return json.loads(s) if s else []


def media_url(path):
    if not path:
        return None
    try:
        rel = str(__import__("pathlib").Path(path).resolve().relative_to(config.data_dir().resolve()))
    except ValueError:
        return None
    return url_for("media", rel=rel)


app.jinja_env.globals["media_url"] = media_url


@app.context_processor
def _nav():
    return {"review_count": db.one("SELECT COUNT(*) n FROM clips WHERE status='pending_review'")["n"],
            "submit_count": db.one("""SELECT COUNT(*) n FROM posts p JOIN campaigns c ON c.id=p.campaign_id
                                      WHERE p.status='posted' AND p.submitted=0
                                      AND (COALESCE(c.url, '') != '' OR c.origin != 'yaml')""")["n"]}


@app.route("/")
def overview():
    lanes = []
    for c in campaigns.all_campaigns():
        s = campaigns.lane_stats(c)
        if s["state"] not in ("suggested", "ignored"):
            lanes.append({"c": c, "s": s})
    totals = {k: sum(l["s"][k] for l in lanes) for k in ("review", "scheduled", "posted", "views", "earned", "moments_total")}
    events = db.q("SELECT e.*, c.name campaign FROM events e LEFT JOIN campaigns c ON c.id=e.campaign_id "
                  "ORDER BY e.id DESC LIMIT 14")
    return render_template("overview.html", lanes=lanes, totals=totals, events=events,
                           watcher_last=db.kv_get("watcher_last_run"), stats_last=db.kv_get("stats_last_run"),
                           all_campaigns=campaigns.all_campaigns())


@app.route("/campaign/<cid>")
def campaign(cid):
    c = campaigns.get(cid) or abort(404)
    videos = db.q("SELECT * FROM videos WHERE campaign_id=? ORDER BY created_at DESC LIMIT 20", cid)
    for v in videos:
        v["moments"] = db.q("""SELECT m.*, k.id clip_id, k.status clip_status FROM moments m
                               LEFT JOIN clips k ON k.moment_id=m.id WHERE m.video_id=? AND m.campaign_id=?
                               ORDER BY m.start_s""", v["id"], cid)
    return render_template("campaign.html", c=c, s=campaigns.lane_stats(c), sources=campaigns.sources(cid),
                           videos=videos, state=campaigns.lane_state(c))


@app.post("/campaign/<cid>/pause")
def pause(cid):
    c = campaigns.get(cid) or abort(404)
    campaigns.set_paused(cid, not c["paused"])
    return redirect(request.referrer or url_for("overview"))


@app.route("/review")
def review_page():
    clips = db.q("""SELECT k.*, m.hook, m.score, m.category, m.why, m.start_s, m.end_s, v.title video_title, v.url video_url,
                    c.name campaign FROM clips k JOIN moments m ON m.id=k.moment_id
                    JOIN videos v ON v.id=m.video_id AND v.campaign_id=k.campaign_id
                    JOIN campaigns c ON c.id=k.campaign_id WHERE k.status='pending_review' ORDER BY m.score DESC, k.id""")
    return render_template("review.html", clips=clips)


@app.post("/review/<int:clip_id>/approve")
def approve(clip_id):
    review.approve(clip_id, int(request.form.get("title_index", 0)), request.form.get("custom_title"))
    return redirect(url_for("review_page"))


@app.post("/review/<int:clip_id>/reject")
def reject(clip_id):
    review.reject(clip_id, request.form.get("reason") or "not good enough")
    return redirect(url_for("review_page"))


@app.route("/posts")
def posts():
    rows = db.q("""SELECT p.*, k.title, c.name campaign, c.url campaign_url, c.rate_per_1k, c.min_views,
                   c.max_payout_per_clip FROM posts p JOIN clips k ON k.id=p.clip_id JOIN campaigns c ON c.id=p.campaign_id
                   ORDER BY p.scheduled_at DESC LIMIT 200""")
    for r in rows:
        r["earned"] = campaigns.post_earning(r, r["views"] or 0)
    return render_template("posts.html", posts=rows)


@app.post("/posts/<int:post_id>/submitted")
def mark_submitted(post_id):
    db.x("UPDATE posts SET submitted=1, submitted_at=? WHERE id=?", iso(utcnow()), post_id)
    return redirect(request.referrer or url_for("posts"))


@app.post("/videos/add")
def add_video():
    try:
        watcher.add_video(request.form["ref"].strip(), request.form["campaign"])
    except (Exception, SystemExit) as e:
        db.log(request.form.get("campaign"), f"Could not add video: {e}", "error")
    return redirect(url_for("overview"))


@app.route("/campaigns")
def campaigns_page():
    rows, suggested = [], []
    for c in campaigns.all_campaigns():
        s = campaigns.lane_stats(c)
        if s["state"] == "ignored":
            continue
        row = {"c": c, "s": s, "sources": campaigns.sources(c["id"])}
        if s["state"] == "suggested":
            row["ok"], row["reasons"] = campaigns.fit(c)
            suggested.append(row)
        else:
            rows.append(row)
    return render_template("campaigns.html", rows=rows, suggested=suggested,
                           finder_last=db.kv_get("discovery_last_run"))


@app.post("/campaigns/<cid>/activate")
def campaign_activate(cid):
    campaigns.activate(cid) or abort(404)
    return redirect(url_for("campaign", cid=cid))


@app.post("/campaigns/<cid>/ignore")
def campaign_ignore(cid):
    campaigns.ignore(cid)
    return redirect(url_for("campaigns_page"))


def _form_from_campaign(c, srcs):
    lines = []
    for s in srcs:
        if s["kind"] == "twitch_channel":
            lines.append(f"https://www.twitch.tv/{s['channel_id']}")
        elif s["kind"] == "folder":
            lines.append(s["channel_id"])
        elif s["channel_id"].startswith("@"):
            lines.append(f"https://www.youtube.com/{s['channel_id']}")
        else:
            lines.append(f"https://www.youtube.com/channel/{s['channel_id']}")
    return {**c, "required_hashtags": " ".join(c["required_hashtags"]), "rules": "\n".join(c["rules"]),
            "sources": "\n".join(lines), "layout": srcs[0]["layout"] if srcs else "blur"}


def _form_from_extract(d):
    lo_hi = d.get("clip_length") or [20, 55]
    return {**d, "min_len": (lo_hi + [None, None])[0] or 20, "max_len": (lo_hi + [None, None])[1] or 55,
            "required_hashtags": " ".join(d.get("required_hashtags") or []),
            "rules": "\n".join(map(str, d.get("rules") or [])), "sources": "\n".join(map(str, d.get("sources") or [])),
            "audience": d.get("audience") or "US viewers aged 16 to 34", "daily_clip_quota": 3, "min_score": 7,
            "layout": "blur"}


@app.route("/campaigns/new")
def campaign_new():
    return render_template("campaign_form.html", f={"platforms": ["youtube"], "audience": "US viewers aged 16 to 34",
                                                    "daily_clip_quota": 3, "min_len": 20, "max_len": 55,
                                                    "min_score": 7, "layout": "blur"}, notes=[], errors=[])


@app.route("/campaigns/<cid>/edit")
def campaign_edit(cid):
    c = campaigns.get(cid) or abort(404)
    notes = ["This campaign comes from campaigns.yaml. Saving here makes the dashboard version the one that counts."] \
        if c["origin"] == "yaml" else []
    return render_template("campaign_form.html", f=_form_from_campaign(c, campaigns.sources(cid)), cid=cid,
                           notes=notes, errors=[])


@app.post("/campaigns/extract")
def campaign_extract():
    brief = request.form.get("brief", "").strip()
    if len(brief) < 40:
        return render_template("campaign_form.html", f={**request.form, "brief": brief}, notes=[],
                               errors=["Paste the full text of the campaign page first."])
    try:
        data, provider = campaigns.extract_from_brief(brief)
    except Exception as e:
        return render_template("campaign_form.html", f={**request.form, "brief": brief}, notes=[],
                               errors=[f"The AI couldn't read it: {e}"])
    notes = [f"Filled in by {provider}. Check every field before saving."]
    if data.get("unsupported_platforms"):
        notes.append("This campaign also accepts " + ", ".join(data["unsupported_platforms"]) +
                     ". The tool posts to YouTube, Instagram and Facebook only.")
    if not data.get("platforms"):
        notes.append("None of the accepted platforms can be posted automatically. YouTube is selected for now.")
        data["platforms"] = ["youtube"]
    return render_template("campaign_form.html", f=_form_from_extract(data), cid=request.form.get("cid") or None,
                           notes=notes, errors=[])


@app.post("/campaigns/save")
def campaign_save():
    f = request.form
    cid = f.get("cid") or None
    data = {k: f.get(k) for k in ("name", "platform", "creator", "url", "topic", "audience", "rate_per_1k",
                                  "budget_left", "max_payout_per_clip", "min_views", "ends", "daily_clip_quota",
                                  "required_hashtags", "credit", "min_score", "rules", "brief")}
    data["platforms"] = f.getlist("platforms")
    data["clip_length"] = [f.get("min_len") or 20, f.get("max_len") or 55]
    srcs, videos, errors = [], [], []
    for line in (f.get("sources") or "").splitlines():
        try:
            parsed = campaigns.parse_source(line)
        except ValueError as e:
            errors.append(str(e))
            continue
        if not parsed:
            continue
        kind, ident = parsed
        if kind == "video":
            videos.append(ident)
        else:
            srcs.append({"kind": kind, "channel_id": ident, "layout": f.get("layout") or "blur",
                         "streams_only": kind == "twitch_channel" or bool(f.get("streams_only"))})
    if not data["name"]:
        errors.append("Give the campaign a name.")
    if errors:
        return render_template("campaign_form.html", f={**f.to_dict(), "platforms": data["platforms"]}, cid=cid,
                               notes=[], errors=errors)
    if not cid:
        base = campaigns.slug(data["name"])
        cid, n = base, 2
        while campaigns.get(cid):
            cid, n = f"{base}-{n}", n + 1
    try:
        campaigns.save(cid, data, "dashboard")
    except (ValueError, TypeError) as e:
        return render_template("campaign_form.html", f={**f.to_dict(), "platforms": data["platforms"]}, cid=cid,
                               notes=[], errors=[f"Check the numbers: {e}"])
    campaigns.set_sources(cid, srcs, f.get("layout") or "blur")
    for v in videos:
        try:
            watcher.add_video(v, cid)
        except (Exception, SystemExit) as e:
            db.log(cid, f"Could not add {v}: {e}", "error")
    db.log(cid, f"Campaign saved with {len(srcs)} sources and {len(videos)} videos")
    return redirect(url_for("campaign", cid=cid))


@app.route("/media/<path:rel>")
def media(rel):
    return send_from_directory(config.data_dir(), rel)


def serve(host="127.0.0.1", port=8787):
    app.run(host=host, port=port, debug=False, threaded=True)
