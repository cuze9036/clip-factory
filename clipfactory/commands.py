"""What you can type in Discord, and what each button does. Plain functions so they're easy to test."""
import re

from . import campaigns, db, review, watcher
from .util import ET, iso, utcnow

HELP = """**Clip Factory commands**
`!status` every lane's numbers
`!review` clips waiting for you · `!approve <clip> [title 1-3]` · `!reject <clip> [reason]` · `!cancel <clip>`
`!todo` clips to post yourself and links to submit · `!posted <post> <link>` · `!submitted <post>`
`!campaign` or `!live` live campaigns with Open / I joined buttons · after "I joined", just paste the campaign page
`!campaign <paste the whole campaign page>` AI reads it and suggests a lane
`!guide [campaign] [text]` the campaign's instructions the AI follows; paste reference material to add it
`!ai` which AI did the work · `!fix` fix what it can now · `!check` problems right now · `!insights` this week's review (also sent by itself every week)
`!board` refresh #campaign-updates and #active-campaigns now · `!suggested` campaigns the finder found · `!cleanup` remove duplicate suggestions
`!campaigns` · `!activate <id>` · `!ignore <id>` · `!pause <id>` · `!resume <id>`
`!source <campaign id> <YouTube/Twitch channel, folder or video link>` tell a campaign where its content is
`!add <video link> <campaign id>` clip one video
`!find` look for new campaigns now · `!emails` Google audit emails from the last 7 days
`!selftest` check every part of the tool (takes 1-5 min) · `!version` which version is running · `!retry` remake failed clips · `!remake <clip>` remake one clip with the current look"""


# Discord slash commands: name -> (what it does, what goes in the "details" box, or None for no box)
SLASH = {
    "campaign": ("Live campaigns with Open / I joined buttons", "optional: paste a campaign page"),
    "status": ("Every lane's numbers", None),
    "review": ("Clips waiting for you", None),
    "approve": ("Approve a clip", "clip number, optional title 1-3"),
    "reject": ("Reject a clip", "clip number, optional reason"),
    "cancel": ("Stop a clip's scheduled posts", "clip number"),
    "todo": ("Clips to post yourself and links to submit", None),
    "posted": ("You posted a clip yourself", "post number and the link"),
    "submitted": ("You submitted a post on the campaign page", "post number"),
    "suggested": ("All campaigns the finder found", None),
    "guide": ("The campaign's guide the AI follows; add reference material", "campaign number/id, then pasted instructions (optional)"),
    "board": ("Refresh #campaign-updates and #active-campaigns now", None),
    "activate": ("Start a campaign's lane", "campaign number or id"),
    "ignore": ("Hide a campaign", "campaign number or id"),
    "pause": ("Pause a lane", "campaign id"),
    "resume": ("Resume a lane", "campaign id"),
    "source": ("Tell a campaign where its content is", "campaign number or id, then the link or folder"),
    "add": ("Clip one video", "video link, then campaign id"),
    "find": ("Look for new campaigns now", None),
    "ai": ("Which AI did the work today (free models vs Gemini)", None),
    "fix": ("Fix what it can right now (retries, Telegram fallback) and explain errors", None),
    "check": ("Daily check now: problems and what to do", None),
    "insights": ("This week's review: what works, what doesn't, what to change", None),
    "emails": ("Google audit emails from the last 7 days", None),
    "campaigns": ("Every campaign and its state", None),
    "selftest": ("Check every part of the tool (1-5 min)", None),
    "version": ("Which version is running", None),
    "retry": ("Remake failed clips", None),
    "remake": ("Remake one clip with the current look", "clip number"),
    "cleanup": ("Remove duplicate suggestions", None),
    "help": ("All commands", None),
}


def version():
    from . import config
    f = config.path("VERSION")
    return f.read_text(encoding="utf-8").strip() if f.exists() else "unknown"


def _clip_line(k):
    return f"#{k['id']} · {k['campaign']} · {k['score']:.0f}/10 · {k['title']}"


def status():
    lines, waiting = [], 0
    for c in campaigns.all_campaigns():
        s = campaigns.lane_stats(c)
        if s["state"] == "suggested":
            waiting += 1
            continue
        if s["state"] == "ignored":
            continue
        lines.append(f"**{c['name']}** `{c['id']}` · {s['state']} · review {s['review']} · scheduled {s['scheduled']}"
                     f" · posted {s['posted']} · {s['views']:,} views · ~${s['earned']:,.2f}")
    out = "\n".join(lines) or "No lanes yet."
    if waiting:
        out += f"\n\n{waiting} found campaign(s) waiting for you. Type `!suggested` to see them."
    return out


def _paid_platforms(c):
    extra = campaigns._extra(c)
    return ([] if extra.get("no_supported_platform") else c["platforms"]) + (extra.get("unsupported_platforms") or [])


def _short_name(name):
    name = re.sub(r"^\$+", "$", name or "").strip()
    return re.sub(r"\s*[-·]?\s*clipping(\s+campaign)?\s*$", "", name, flags=re.I) or name


def _money(v):
    return f"${v / 1000:.1f}K".replace(".0K", "K") if v >= 1000 else f"${v:,.0f}"


def _where(c):
    """'YouTube auto + Instagram you post' for the platforms this campaign can actually use."""
    from . import publish
    parts = []
    for p in c["platforms"]:
        m = publish.mode(p, c["accounts"].get(p, "main"))
        if m:
            parts.append((m != "auto", f"{publish.name(p)} ({'auto' if m == 'auto' else 'you post'})"))
    return " + ".join(t for _, t in sorted(parts)) or "no platform you can use yet"


def suggested():
    from . import discovery
    discovery.refresh_from_listings()
    discovery.backfill_links()
    rows = []
    for c in campaigns.all_campaigns():
        if campaigns.lane_state(c) != "suggested":
            continue
        ok, _ = campaigns.fit(c)
        clippers = campaigns._extra(c).get("clippers") or 0
        auto = "youtube" in c["platforms"]
        rows.append(((not ok, not c["rate_per_1k"], not auto, -c["rate_per_1k"], clippers), c, ok, clippers))
    if not rows:
        return "No found campaigns waiting. The finder looks again every few hours, or type `!find`."
    rows.sort(key=lambda r: r[0])
    db.kv_set("suggested_order", [r[1]["id"] for r in rows])
    out = ["**Found campaigns** (best first)"]
    for n, (_, c, ok, clippers) in enumerate(rows, 1):
        pay = f"${c['rate_per_1k']:.2f} per 1K" if c["rate_per_1k"] else "pay: check on Whop"
        bits = [pay]
        if c["budget_left"] is not None:
            bits.append(f"{_money(c['budget_left'])} left")
        if clippers:
            bits.append(f"{clippers:,} clippers")
        out.append(f"**{n}. {_short_name(c['name'])}**{'' if ok else ' (weak fit)'}\n"
                   f"    {' · '.join(bits)} · {_where(c)}")
        if c["url"]:
            out.append(f"    {c['url']}")
    out.append("\n**To start one:** type `!campaign` for cards with Open / I joined buttons, join on Whop, tap "
               "I joined and paste the campaign page. Not interested: `!ignore 1`.")
    return "\n".join(out)


def _resolve(arg):
    """A number from the last !suggested list, or a campaign id."""
    if arg.isdigit():
        order = db.kv_get("suggested_order") or []
        if 1 <= int(arg) <= len(order):
            return order[int(arg) - 1]
    return arg


def review_list():
    rows = db.q("""SELECT k.id, k.title, m.score, c.name campaign FROM clips k JOIN moments m ON m.id=k.moment_id
                   JOIN campaigns c ON c.id=k.campaign_id WHERE k.status='pending_review' ORDER BY m.score DESC""")
    return "\n".join(_clip_line(k) for k in rows) or "Nothing waiting for review."


def todo():
    manual = db.q("""SELECT p.id, p.platform, c.name campaign FROM posts p JOIN campaigns c ON c.id=p.campaign_id
                     WHERE p.status='manual' ORDER BY p.scheduled_at""")
    head = ""
    if manual:
        head = "**To post yourself** (files in Clipping Tools\\Ready to post):\n" + "\n".join(
            f"post {r['id']} · {r['campaign']} · {r['platform']} → then `!posted {r['id']} <link>`" for r in manual) + "\n\n"
    rows = db.q("""SELECT p.id, p.platform, p.url, c.name campaign, c.url curl FROM posts p
                   JOIN campaigns c ON c.id=p.campaign_id
                   WHERE p.status='posted' AND p.submitted=0 AND c.url != '' ORDER BY p.posted_at""")
    body = "\n".join(f"post {r['id']} · {r['campaign']} · {r['platform']}: {r['url']} → submit at {r['curl']}"
                     for r in rows)
    return (head + ("**To submit:**\n" + body if body else "Nothing to submit.")).strip()


def approve(clip_id, title_no=1):
    when = review.approve(clip_id, max(0, title_no - 1))
    if not when:
        return f"Clip #{clip_id} isn't waiting for review."
    return f"Clip #{clip_id} approved. Goes live {when.astimezone(ET):%a %b %d, %I:%M %p} ET."


def reject(clip_id, reason="rejected on Discord"):
    clip = db.one("SELECT status FROM clips WHERE id=?", clip_id)
    if not clip or clip["status"] != "pending_review":
        return f"Clip #{clip_id} isn't waiting for review."
    review.reject(clip_id, reason)
    return f"Clip #{clip_id} rejected."


def cancel(clip_id):
    stopped, uploaded = review.cancel(clip_id)
    msg = f"Stopped {stopped} scheduled post(s) of clip #{clip_id}."
    if uploaded:
        msg += " Its YouTube upload is already scheduled on YouTube; delete it in YouTube Studio."
    return msg


def submitted(post_id):
    if not db.one("SELECT 1 FROM posts WHERE id=?", post_id):
        return f"No post {post_id}."
    db.x("UPDATE posts SET submitted=1, submitted_at=? WHERE id=?", iso(utcnow()), post_id)
    return f"Post {post_id} marked submitted."


def campaign_list():
    rows = []
    for c in campaigns.all_campaigns():
        state = campaigns.lane_state(c)
        if state == "ignored":
            continue
        rows.append(f"`{c['id']}` · {c['name']} · {state} · ${c['rate_per_1k']:.2f}/1K")
    return "\n".join(rows) or "No campaigns yet."


def activate(cid):
    c = campaigns.activate(cid)
    if not c:
        return f"No campaign `{cid}`."
    if not campaigns.sources(cid) and not db.one("SELECT 1 FROM videos WHERE campaign_id=?", cid):
        return (f"**{c['name']}** is active, but it has no content sources yet. Add one on the dashboard, "
                f"or type `!add <video link> {cid}`.")
    return f"**{c['name']}** is active. Its lane starts on the next pass."


def ignore(cid):
    if not campaigns.get(cid):
        return f"No campaign `{cid}`."
    campaigns.ignore(cid)
    return f"Ignored `{cid}`."


def handle(text):
    """One typed message -> reply text. Returns None for messages that aren't commands."""
    text = re.sub(r"^[!/]\s*", "!", (text or "").strip())  # "/status", "! Pause" and "!status" all work
    text = re.sub(r"^(![A-Za-z_]+)@\S+", r"\1", text)  # "/status@ClipBot"
    if not text.startswith("!"):
        return None
    cmd, _, rest = text.partition(" ")
    cmd, rest = cmd.lower(), rest.strip()
    args = rest.split()
    try:
        if cmd == "!help":
            return HELP
        if cmd == "!status":
            return status()
        if cmd == "!review":
            return review_list()
        if cmd == "!todo":
            return todo()
        if cmd == "!campaigns":
            return campaign_list()
        if cmd == "!suggested":
            return suggested()
        if args and cmd in ("!raw", "!activate", "!ignore", "!source", "!pause", "!resume"):
            args[0] = _resolve(args[0])
        if cmd == "!raw":
            c = campaigns.get(args[0])
            if not c:
                return f"No campaign `{args[0]}`."
            return "Original listing for `" + args[0] + "`:\n```\n" + (c.get("brief") or "(nothing saved)")[:1700] + "\n```"
        if cmd == "!cleanup":
            n = campaigns.remove_duplicate_suggestions()
            return f"Removed {n} duplicate suggestion(s)." if n else "No duplicates found."
        if cmd == "!approve":
            return approve(int(args[0]), int(args[1]) if len(args) > 1 else 1)
        if cmd == "!reject":
            return reject(int(args[0]), " ".join(args[1:]) or "rejected on Discord")
        if cmd == "!cancel":
            return cancel(int(args[0]))
        if cmd == "!submitted":
            return submitted(int(args[0]))
        if cmd == "!activate":
            return activate(args[0])
        if cmd == "!ignore":
            return ignore(args[0])
        if cmd in ("!pause", "!resume"):
            if not campaigns.get(args[0]):
                return f"No campaign `{args[0]}`."
            campaigns.set_paused(args[0], cmd == "!pause")
            return f"`{args[0]}` {'paused' if cmd == '!pause' else 'resumed'}."
        if cmd == "!posted":
            from . import publish
            post = db.one("SELECT * FROM posts WHERE id=?", int(args[0]))
            if not post:
                return f"No post {args[0]}."
            if len(args) < 2 or not args[1].startswith("http"):
                return f"Add the link: `!posted {args[0]} https://...`"
            publish.mark_posted(post["id"], args[1])
            return f"Post {args[0]} marked live."
        if cmd == "!source":
            from . import joinflow
            if not args:
                return "Send `!source <campaign number> <YouTube/Twitch channel, video link or folder>`."
            cid = _resolve(args[0])
            if campaigns.get(cid) and len(args) > 1:
                return joinflow.add_link(cid, " ".join(args[1:]))
            # Just a link: the campaign waiting for it, or the only running one.
            cid, question = _which()
            if not cid:
                return question
            return joinflow.add_link(cid, rest)
        if cmd == "!add":
            vid = watcher.add_video(args[0], args[1])
            return f"Added `{vid}` to `{args[1]}`. The AI will look for moments on the next pass."
        if cmd == "!live" or (cmd == "!campaign" and not rest):
            from . import joinflow
            return joinflow.post_live()
        if cmd == "!campaign":
            from . import joinflow
            if len(rest) < 40:
                return "Paste the whole campaign page after `!campaign`, or type `!campaign` alone to see live ones."
            if joinflow.awaiting():
                return joinflow.apply_brief(joinflow.awaiting(), rest)
            from . import discovery
            cid, ok = discovery.add_from_text(rest, origin="pasted")
            return None  # the suggestion message with buttons comes from the outbox
        if cmd == "!remake":
            clip = db.one("SELECT * FROM clips WHERE id=?", int(args[0]))
            if not clip or not clip["raw_path"]:
                return f"No clip {args[0]} to remake."
            if db.one("SELECT 1 FROM posts WHERE clip_id=? AND status IN ('posted','uploaded')", clip["id"]):
                return f"Clip {args[0]} is already live, so it can't be remade."
            db.x("DELETE FROM posts WHERE clip_id=? AND status='queued'", clip["id"])
            db.x("UPDATE clips SET status='downloaded', error=NULL WHERE id=?", clip["id"])
            return f"Remaking clip {args[0]} with the current layout. It'll show up here in about 5-15 minutes."
        if cmd == "!retry":
            clips = db.q("SELECT id FROM clips WHERE status='failed' AND raw_path IS NOT NULL")
            moms = db.q("SELECT id FROM moments WHERE status='failed'")
            db.x("UPDATE clips SET status='downloaded', error=NULL WHERE status='failed' AND raw_path IS NOT NULL")
            db.x("UPDATE moments SET status='found', error=NULL WHERE status='failed'")
            if not clips and not moms:
                return "Nothing failed, nothing to retry."
            return (f"Retrying {len(clips)} clip(s) and {len(moms)} moment(s). "
                    "They'll show up here in about 5-15 minutes.")
        if cmd == "!selftest":
            from . import selftest
            return selftest.run_all()
        if cmd == "!version":
            return f"Clip Factory version {version()}"
        if cmd == "!board":
            db.kv_set("board_dirty", True)
            for k in ("live", "running"):
                db.kv_set(f"board_last_full:{k}", None)
            return "Refreshing #campaign-updates and #active-campaigns (about a minute)."
        if cmd == "!guide":
            from . import guide, joinflow
            first = _resolve(args[0]) if args else ""
            text = rest.partition(" ")[2] if campaigns.get(first) else rest
            cid, question = _which(first)
            if not cid:
                return question
            if len(text.strip()) >= 20:
                joinflow.done_waiting()
                return guide.add(cid, text)
            return guide.show(cid)
        if cmd == "!ai":
            from . import llm
            return llm.usage(int(args[0]) if args and args[0].isdigit() else 1)
        if cmd == "!fix":
            from . import autofix
            done, notes = autofix.run()
            if not done and not notes:
                return "Nothing to fix right now."
            return "Done. Details are in #ai-insights."
        if cmd == "!check":
            from . import health
            return health.report()
        if cmd == "!insights":
            from . import insights
            return insights.run(send=False)
        if cmd == "!emails":
            from . import gmail_watch
            return gmail_watch.emails_command()
        if cmd == "!find":
            from . import discovery
            db.kv_set("discovery_last_run", None)
            n = discovery.run()
            return f"Found {n} new campaign(s) worth a look." if n else "No new campaigns that fit right now."
    except (IndexError, ValueError):
        return "Missing or wrong details. Type `!help`."
    except SystemExit as e:
        return str(e)
    except Exception as e:
        db.log(None, f"Command {cmd} failed: {e}", "error")
        return f"That didn't work: {str(e)[:300]}"
    return "Unknown command. Type `!help`."


def _which(arg_cid=None):
    """The campaign a command means when you didn't name one: the one waiting for your paste, else the only
    running one. With several running, returns (None, a question listing them)."""
    from . import joinflow
    if arg_cid and campaigns.get(arg_cid):
        return arg_cid, None
    cid = joinflow.awaiting()
    if cid:
        return cid, None
    running = [c for c in campaigns.all_campaigns() if c.get("stage") == "active" and not c["paused"]
               and c.get("origin") != "yaml"]
    if len(running) == 1:
        return running[0]["id"], None
    if not running:
        last = joinflow.last_joined()
        return (last, None) if last else (None, "Which campaign? Tap **I joined** on its card first.")
    names = "\n".join(f"• `{c['id']}` {c['name']}" for c in running)
    return None, ("Which campaign? Tap **📋 Guide** or **🎬 Footage** on its card in #active-campaigns, "
                  f"or put its id first:\n{names}")


def pasted(cid, text):
    """Something you pasted while a campaign was waiting for it: its page (after I joined), a guide or footage
    (after the 📋 Guide / 🎬 Footage buttons). The reply always names the campaign."""
    from . import guide, joinflow
    kind = joinflow.awaiting_kind() or "brief"
    if kind == "guide":
        try:
            reply = guide.add(cid, text)
        except Exception as e:
            return f"Couldn't read that guide for **{campaigns.get(cid)['name']}**: {str(e)[:400]}"
        joinflow.done_waiting()
        return reply
    if kind == "source":
        links = re.findall(r"https?://\S+|[A-Za-z]:\\[^\n]+|@[\w.-]+", text)
        if not links:
            return "Send a Dropbox / YouTube / Twitch link or a folder path."
        c = campaigns.get(cid)
        replies = [joinflow.add_link(cid, link.strip()) for link in links[:5]]
        joinflow.done_waiting()
        return f"**{c['name']}**:\n" + "\n".join(replies)
    return brief(cid, text)


def card_button(kind, cid):
    """📋 Guide / 🎬 Footage / ⏸ Pause on a campaign card in #active-campaigns."""
    from . import guide, joinflow
    c = campaigns.get(cid)
    if not c:
        return f"No campaign `{cid}`."
    if kind == "guide":
        joinflow.expect(cid, "guide")
        return (guide.show(cid) + f"\n\n**Now paste the guide / reference material for {c['name']}** "
                "(next 30 minutes; a long paste can be a file). It's added to this campaign only.")
    if kind == "src":
        joinflow.expect(cid, "source")
        srcs = campaigns.sources(cid)
        have = "\n".join(f"• {s['kind'].replace('_', ' ')}: {s['name'][:80]}" for s in srcs) or "• none yet"
        return (f"**Footage for {c['name']}**\n{have}\n\n**Now send the Dropbox / YouTube / Twitch link or "
                "folder path** for this campaign (next 30 minutes).")
    if kind == "pause":
        paused = not c["paused"]
        campaigns.set_paused(cid, paused)
        return f"**{c['name']}** {'paused' if paused else 'running again'}."
    return "That button has expired."


def brief(cid, text):
    """The campaign page (or a footage link) pasted after tapping "I joined"."""
    from . import joinflow
    if len(text) < 300 and re.fullmatch(r"\s*<?(https?://\S+|[A-Za-z]:\\.+|@[\w.-]+)>?\s*", text):
        return joinflow.add_link(cid, text)
    try:
        return joinflow.apply_brief(cid, text)
    except Exception as e:
        db.log(cid, f"Reading the pasted campaign page failed: {e}", "error")
        return f"Couldn't read that page: {str(e)[:300]}. Try pasting it again, or use `!source {cid} <link>`."


def button(custom_id):
    """A tapped button -> reply text."""
    try:
        return _button(custom_id)
    except Exception as e:
        db.log(None, f"Button {custom_id} failed: {e}", "error")
        return f"That didn't work: {str(e)[:300]}"


def _button(custom_id):
    kind, _, rest = custom_id.partition(":")
    if kind == "a":
        clip_id, idx = rest.split(":")
        return approve(int(clip_id), int(idx) + 1)
    if kind == "r":
        return reject(int(rest))
    if kind == "x":
        return cancel(int(rest))
    if kind == "sub":
        return submitted(int(rest))
    if kind == "skip":
        db.x("UPDATE posts SET status='failed', error='skipped by you' WHERE id=?", int(rest))
        return f"Skipped post {rest}."
    if kind == "act":
        return activate(rest)
    if kind in ("guide", "src", "pause"):
        return card_button(kind, rest)
    if kind == "join":
        from . import joinflow
        return joinflow.joined(rest)
    if kind == "ign":
        return ignore(rest)
    return "That button has expired."
