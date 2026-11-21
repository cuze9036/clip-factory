"""What you can type in Discord, and what each button does. Plain functions so they're easy to test."""
import re

from . import campaigns, db, review, watcher
from .util import ET, iso, utcnow

HELP = """**Clip Factory commands**
`!status` every lane's numbers
`!review` clips waiting for you · `!approve <clip> [title 1-3]` · `!reject <clip> [reason]` · `!cancel <clip>`
`!todo` clips to post yourself and links to submit · `!posted <post> <link>` · `!submitted <post>`
`!campaign <paste the whole campaign page>` AI reads it and suggests a lane
`!suggested` campaigns the finder found · `!cleanup` remove duplicate suggestions
`!campaigns` · `!activate <id>` · `!ignore <id>` · `!pause <id>` · `!resume <id>`
`!source <campaign id> <YouTube/Twitch channel, folder or video link>` tell a campaign where its content is
`!add <video link> <campaign id>` clip one video
`!find` look for new campaigns now
`!selftest` check every part of the tool (takes 1-5 min) · `!version` which version is running · `!retry` remake failed clips · `!remake <clip>` remake one clip with the current look"""


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
            parts.append((m != "auto", f"{p.title().replace('Youtube', 'YouTube')} ({'auto' if m == 'auto' else 'you post'})"))
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
        return "No found campaigns waiting. The finder looks again every 6 hours, or type `!find`."
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
    out.append("\n**To start one:** search its name on Whop and join, copy the content link it gives you, then\n"
               "`!source 1 <link>` and `!activate 1` (use the campaign's number). Not interested: `!ignore 1`.")
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
        if args and cmd in ("!raw", "!activate", "!ignore", "!source"):
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
            if not campaigns.get(args[0]):
                return f"No campaign `{args[0]}`."
            return campaigns.add_source(args[0], " ".join(args[1:]))
        if cmd == "!add":
            vid = watcher.add_video(args[0], args[1])
            return f"Added `{vid}` to `{args[1]}`. The AI will look for moments on the next pass."
        if cmd == "!campaign":
            if len(rest) < 40:
                return "Paste the whole campaign page after `!campaign`."
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
    if kind == "ign":
        return ignore(rest)
    return "That button has expired."
