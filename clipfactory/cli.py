import argparse
import os
import sys

from . import campaigns, db


def main(argv=None):
    p = argparse.ArgumentParser(prog="clipfactory", description="Clipping-campaign automation: every campaign runs in its own lane")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync", help="load campaigns.yaml into the database")
    sub.add_parser("status", help="print every lane's numbers")
    r = sub.add_parser("run", help="run the whole pipeline forever")
    r.add_argument("--pause", type=int, default=30, help="seconds between passes")
    sub.add_parser("once", help="run one pass of every stage")
    sub.add_parser("watch", help="check sources for ended streams and new uploads now")
    a = sub.add_parser("add-video", help="add a YouTube URL or a local file to a lane")
    a.add_argument("ref")
    a.add_argument("--campaign", required=True)
    l = sub.add_parser("clip-local", help="Phase 1: turn a local recording into clips right now")
    l.add_argument("file")
    l.add_argument("--campaign", required=True)
    sub.add_parser("find-campaigns", help="look for new clipping campaigns now")
    sub.add_parser("selftest", help="check every part of the tool on this computer")
    sub.add_parser("publish", help="upload posts that are due")
    sub.add_parser("stats", help="refresh views now")
    au = sub.add_parser("auth-youtube", help="connect a YouTube channel")
    au.add_argument("account", nargs="?", default="main")
    d = sub.add_parser("dashboard", help="open the local dashboard")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8787)
    dm = sub.add_parser("demo", help="open a demo dashboard with sample lanes and real rendered clips")
    dm.add_argument("--port", type=int, default=8788)
    dm.add_argument("--no-serve", action="store_true")
    args = p.parse_args(argv)
    if args.cmd == "demo":
        os.environ["CLIPFACTORY_DATA"] = "data-demo"

    if args.cmd == "sync":
        print(f"Synced {campaigns.sync()} campaigns")
    elif args.cmd == "status":
        for c in campaigns.all_campaigns():
            s = campaigns.lane_stats(c)
            print(f"{c['name']:<34} {s['state']:<12} streams {s['videos']:>3}  moments {s['moments_total']:>3}  "
                  f"review {s['review']:>2}  scheduled {s['scheduled']:>2}  posted {s['posted']:>3}  "
                  f"views {s['views']:>9,}  est ${s['earned']:,.2f}")
    elif args.cmd == "run":
        import threading
        from . import discord_bot, pipeline
        if discord_bot.configured():
            threading.Thread(target=pipeline.run_forever, args=(args.pause,), daemon=True).start()
            print("Pipeline running. Discord bot connecting...")
            discord_bot.run()
        else:
            print("Discord not set up (DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, DISCORD_OWNER_ID); running without it.")
            pipeline.run_forever(args.pause)
    elif args.cmd == "once":
        from . import pipeline
        pipeline.run_once()
    elif args.cmd == "watch":
        from . import watcher
        print(f"{watcher.check_sources()} new videos")
    elif args.cmd == "add-video":
        from . import watcher
        print("Added", watcher.add_video(args.ref, args.campaign))
    elif args.cmd == "clip-local":
        from . import pipeline, watcher
        c = campaigns.get(args.campaign) or sys.exit(f"Unknown campaign {args.campaign}")
        watcher.add_video(args.file, c["id"])
        pipeline.stage_analyze(c)
        for _ in range(c["max_moments_per_video"]):
            pipeline.stage_download(c, limit=1)
            pipeline.stage_render(c)
            pipeline.stage_metadata(c)
        for k in db.q("SELECT final_path, title FROM clips WHERE campaign_id=? AND status='pending_review'", c["id"]):
            print(f"{k['final_path']}  |  {k['title']}")
    elif args.cmd == "selftest":
        from . import selftest
        print(selftest.run_all())
    elif args.cmd == "find-campaigns":
        from . import discovery
        db.kv_set("discovery_last_run", None)
        print(f"{discovery.run()} good campaigns suggested")
    elif args.cmd == "publish":
        from . import publish
        print(f"{publish.run_due()} posts uploaded")
    elif args.cmd == "stats":
        from . import analytics
        print(f"Updated {analytics.refresh()} posts")
    elif args.cmd == "auth-youtube":
        from . import publish
        publish.youtube_auth(args.account)
    elif args.cmd == "dashboard":
        from . import web
        print(f"Dashboard: http://{args.host}:{args.port}")
        web.serve(args.host, args.port)
    elif args.cmd == "demo":
        from . import demo, web
        demo.load()
        if not args.no_serve:
            print(f"Demo dashboard: http://127.0.0.1:{args.port}  (real data stays in data/)")
            web.serve("127.0.0.1", args.port)


if __name__ == "__main__":
    main()
