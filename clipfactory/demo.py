"""Demo lanes so you can see the dashboard before connecting real accounts.
Every name, number and moment here is made up and marked 'demo data' in the dashboard."""
import datetime as dt
import json

from . import config, db, render
from .util import iso, run, utcnow

LANES = [
    dict(id="demo-streamer", name="Streamer GTA 6 clips", platform="Whop", creator="Example Streamer",
         topic="GTA 6 livestreams", url="https://whop.com/", rate_per_1k=1.0,
         budget_left=4820, max_payout_per_clip=300, min_views=1000, daily_clip_quota=3,
         platforms=["youtube", "instagram"], hashtags=["#GTA6"], credit="Credit: @ExampleStreamer", priority=1),
    dict(id="demo-podcast", name="Podcast highlights", platform="Whop", creator="Example Podcast",
         topic="comedy podcast", url="https://whop.com/", rate_per_1k=1.5, budget_left=9400, max_payout_per_clip=500,
         min_views=5000, daily_clip_quota=2, platforms=["youtube", "instagram", "facebook"], hashtags=["#podcast"],
         credit="Credit: @ExamplePodcast", priority=2,
         rules=["No clips from the sponsor read", "Show the guest's name on screen"]),
    dict(id="demo-reactions", name="GTA 6 reactions campaign", platform="Whop", creator="Example Creator",
         topic="GTA 6 reaction videos", url="https://whop.com/", rate_per_1k=0.75,
         budget_left=1260, max_payout_per_clip=150, min_views=2000, daily_clip_quota=2,
         platforms=["youtube"], hashtags=["#GTA6", "#GTAVI"], credit="Credit: @ExampleCreator", priority=3),
    dict(id="demo-own", name="Own gameplay channel", platform="", creator="", topic="GTA 6 gameplay", url="",
         rate_per_1k=0, budget_left=None,
         max_payout_per_clip=None, min_views=0, daily_clip_quota=2, platforms=["youtube"],
         hashtags=["#GTA6"], credit="", priority=5, paused=1),
]

VIDEOS = [
    ("demo-streamer", "dmo_stream1", "GTA 6 DAY ONE, 7 HOUR STREAM (Vice City is insane)", 1, 25380, "analyzed", "gemini-video"),
    ("demo-streamer", "dmo_stream2", "GTA 6 cops are NOT okay (late night stream)", 1, 14220, "analyzing", None),
    ("demo-reactions", "dmo_react1", "Reacting to every GTA 6 easter egg we found", 0, 2460, "analyzed", "gemini-video"),
    ("demo-podcast", "dmo_pod1", "Episode 212 with a stand-up comic (Twitch VOD)", 1, 5400, "analyzed", "transcript"),
]

MOMENTS = [
    ("demo-streamer", "dmo_stream1", 2468, 2509, 9, "crazy", "COPS FLIPPED THE TANK", "Five police cars chase him into a gas station and the whole block explodes.", "posted"),
    ("demo-streamer", "dmo_stream1", 5121, 5160, 9, "funny", "HE TRUSTED THE ALLIGATOR", "He tries to ride an alligator in the Everglades and it drags him under.", "pending_review"),
    ("demo-streamer", "dmo_stream1", 8840, 8873, 8, "fail", "WORST HEIST EVER", "The getaway car runs out of gas ten feet from the bank.", "pending_review"),
    ("demo-streamer", "dmo_stream1", 11502, 11548, 8, "reaction", "HE SAW THE PRICE", "His reaction to the price of the first supercar in the dealership.", "posted"),
    ("demo-streamer", "dmo_stream1", 16310, 16344, 7, "clutch", "LAST SECOND ESCAPE", "Jumps the drawbridge as it rises with three stars.", "scheduled"),
    ("demo-streamer", "dmo_stream1", 21077, 21109, 7, "shock", "NPC REMEMBERED HIM", "A random NPC calls him out for something he did three hours earlier.", "found"),
    ("demo-reactions", "dmo_react1", 312, 351, 9, "shock", "THIS WAS IN THE TRAILER", "They spot the trailer billboard hidden in the opening mission.", "pending_review"),
    ("demo-reactions", "dmo_react1", 1288, 1322, 8, "funny", "THE PHONE CALL", "Lucia's mom calls in the middle of the shootout.", "posted"),
    ("demo-podcast", "dmo_pod1", 1834, 1880, 9, "story", "HE GOT KICKED OFF STAGE", "The guest tells how a heckler got him banned from a club.", "posted"),
    ("demo-podcast", "dmo_pod1", 3920, 3961, 8, "hot_take", "TIPPING IS OUT OF CONTROL", "The hosts argue about tipping at self-checkout.", "found"),
]

WORDS = ("no way no way bro look at this the cops just flipped the whole thing "
         "chat did you see that I am never doing that again").split()


def _source_video():
    out = config.data_dir() / "demo" / "source.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000", "-t", "30",
             "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", out])
    return out


def _fake_words(seconds):
    words, t = [], 0.4
    for w in WORDS:
        if t > seconds - 1:
            break
        words.append({"w": w, "s": round(t, 2), "e": round(t + 0.32, 2)})
        t += 0.42
    return words


def clear():
    for r in db.q("SELECT id FROM campaigns WHERE demo=1"):
        db.x("DELETE FROM events WHERE campaign_id=?", r["id"])
        db.x("DELETE FROM campaigns WHERE id=?", r["id"])
    db.x("DELETE FROM outbox")
    db.x("DELETE FROM videos WHERE id LIKE 'dmo_%'")
    print("Demo data removed")


def load():
    clear()
    now = utcnow()
    for l in LANES:
        db.x("""INSERT INTO campaigns(id, name, platform, creator, topic, rules, url, rate_per_1k, budget_left,
                max_payout_per_clip, min_views, ends, daily_clip_quota, platforms, accounts, required_hashtags, credit,
                priority, paused, demo, origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'demo')""",
             l["id"], l["name"], l["platform"], l["creator"], l["topic"], json.dumps(l.get("rules", [])), l["url"], l["rate_per_1k"], l["budget_left"], l["max_payout_per_clip"], l["min_views"],
             "2026-12-31", l["daily_clip_quota"], json.dumps(l["platforms"]),
             json.dumps({p: "main" for p in l["platforms"]}), json.dumps(l["hashtags"]), l["credit"], l["priority"],
             l.get("paused", 0))
    db.x("""INSERT INTO sources(campaign_id, name, channel_id, streams_only, layout, facecam, last_checked)
            VALUES('demo-streamer','Example Streamer','UCdemo0000000000000000001',1,'facecam_split','[0.70,0.02,0.28,0.30]',?)""",
         iso(now - dt.timedelta(minutes=6)))
    db.x("""INSERT INTO sources(campaign_id, name, channel_id, streams_only, layout, last_checked)
            VALUES('demo-reactions','Example Creator','UCdemo0000000000000000002',0,'blur',?)""",
         iso(now - dt.timedelta(minutes=6)))
    db.x("""INSERT INTO sources(campaign_id, kind, name, channel_id, streams_only, layout, last_checked)
            VALUES('demo-podcast','twitch_channel','examplepodcast','examplepodcast',1,'facecam_split',?)""",
         iso(now - dt.timedelta(minutes=6)))
    db.x("""INSERT INTO sources(campaign_id, kind, name, channel_id, streams_only, layout, last_checked)
            VALUES('demo-podcast','folder','Shared Drive folder','~/Google Drive/Example Podcast raw',0,'blur',?)""",
         iso(now - dt.timedelta(minutes=6)))
    for cid, vid, title, stream, dur, status, route in VIDEOS:
        db.x("""INSERT INTO videos(id, campaign_id, source_id, title, url, is_stream, duration_s, status, route,
                created_at, analyzed_at) VALUES(?,?,(SELECT id FROM sources WHERE campaign_id=?),?,?,?,?,?,?,?,?)""",
             vid, cid, cid, title,
             "https://www.twitch.tv/videos/demo212" if cid == "demo-podcast" else f"https://www.youtube.com/watch?v={vid}",
             stream, dur, status, route,
             iso(now - dt.timedelta(hours=3)), iso(now - dt.timedelta(hours=2)))

    src = _source_video()
    cfg = config.section("render")
    posted_views = iter([48210, 21930, 12904, 3120])
    slot = iter([now - dt.timedelta(hours=h) for h in (30, 26, 21, 6)])
    for i, (cid, vid, s, e, score, cat, hook, why, status) in enumerate(MOMENTS):
        mid = db.x("""INSERT INTO moments(video_id, campaign_id, start_s, end_s, score, category, hook, why, status,
                      created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""", vid, cid, s, e, score, cat, hook.title(), why,
                   "found" if status == "found" else "downloaded", iso(now))
        if status == "found":
            continue
        titles = [f"{hook.title()} in GTA 6", f"GTA 6: {hook.capitalize()}", "Nobody expected this in GTA 6"]
        tags = json.dumps(["#GTA6", "#GTAVI", "#Shorts"])
        desc = f"{why}\n\nCredit: demo\n\n#GTA6 #GTAVI"
        final = None
        if status == "pending_review":
            raw = config.data_dir() / "clips" / cid / f"m{mid}_raw.mp4"
            raw.parent.mkdir(parents=True, exist_ok=True)
            run(["ffmpeg", "-y", "-v", "error", "-ss", str(i % 3 * 4), "-t", "14", "-i", src, "-c", "copy", raw])
            final = str(raw).replace("_raw.mp4", "_final.mp4")
            old = cfg.get("preset")
            cfg["preset"] = "veryfast"
            layout = ("facecam_split", [0.70, 0.02, 0.28, 0.30]) if cid == "demo-streamer" else ("blur", None)
            render.render(raw, final, _fake_words(14), hook, *layout)
            cfg["preset"] = old
        cid_clip = db.x("""INSERT INTO clips(moment_id, campaign_id, final_path, duration_s, titles, title, description,
                           hashtags, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        mid, cid, final, e - s, json.dumps(titles), titles[0], desc, tags, status, iso(now))
        camp = next(l for l in LANES if l["id"] == cid)
        if status == "posted":
            when, views = next(slot), next(posted_views)
            for n, p in enumerate(camp["platforms"]):
                pv = views if n == 0 else int(views * 0.38)
                db.x("""INSERT INTO posts(clip_id, campaign_id, platform, account, scheduled_at, posted_at, external_id,
                        url, status, views, likes, us_share, stats_at, submitted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     cid_clip, cid, p, "main", iso(when), iso(when), f"demo{cid_clip}{p}",
                     f"https://www.youtube.com/shorts/demo{cid_clip}" if p == "youtube" else "https://www.instagram.com/",
                     "posted", pv, pv // 21, 0.61, iso(now - dt.timedelta(hours=1)), int(views > 20000))
        elif status == "scheduled":
            for p, h in zip(camp["platforms"], (5, 6.5)):
                db.x("""INSERT INTO posts(clip_id, campaign_id, platform, account, scheduled_at, status)
                        VALUES(?,?,?,?,?,'queued')""", cid_clip, cid, p, "main", iso(now + dt.timedelta(hours=h)))

    from . import campaigns
    for sug in [
        dict(name="IRL stream clips", platform="Whop", creator="Example IRL Streamer", topic="IRL livestreams",
             url="https://whop.com/", rate_per_1k=2.5, budget_left=12400, min_views=10000,
             platforms=["youtube", "instagram"], sources=["https://www.twitch.tv/exampleirl"], rules=[]),
        dict(name="Finance podcast shorts", platform="Whop", creator="Example Money Show", topic="finance podcast",
             url="https://whop.com/", rate_per_1k=0.4, budget_left=300, min_views=5000, platforms=["instagram"],
             sources=["https://www.youtube.com/@examplemoney"], rules=[]),
    ]:
        sid = campaigns.suggest(sug, "discovered")
        db.x("UPDATE campaigns SET demo=1 WHERE id=?", sid)
    for mins, cid, msg, lvl in [
        (2, None, "Campaign finder: 2 new campaigns read, 1 good fit (IRL stream clips)", "info"),
        (1, "demo-streamer", "Clip ready for review: He Trusted The Alligator in GTA 6", "info"),
        (4, "demo-reactions", "Clip ready for review: This Was In The Trailer in GTA 6", "info"),
        (9, "demo-streamer", "Stream ended: GTA 6 cops are NOT okay (late night stream) (237 min)", "info"),
        (11, "demo-streamer", "gemini watched 6:30:00-7:03:00 of GTA 6 DAY ONE, 7 HOUR STREAM: 2 moments", "info"),
        (12, "demo-streamer", "6 moments kept from GTA 6 DAY ONE, 7 HOUR STREAM (route: gemini-video, model: gemini)", "info"),
        (40, "demo-streamer", "Youtube scheduled: https://www.youtube.com/shorts/demo", "info"),
        (95, "demo-reactions", "free-llm wrote metadata after gemini hit its rate limit", "warn"),
    ]:
        db.x("INSERT INTO events(at, campaign_id, level, msg) VALUES(?,?,?,?)",
             iso(now - dt.timedelta(minutes=mins)), cid, lvl, msg)
    db.kv_set("watcher_last_run", iso(now - dt.timedelta(minutes=6)))
    db.kv_set("stats_last_run", iso(now - dt.timedelta(hours=1)))
    db.kv_set("discovery_last_run", iso(now - dt.timedelta(minutes=2)))
    print("Demo lanes loaded into data-demo/")
