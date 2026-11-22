# Clip Factory

Owner writes Hindi/Hinglish: reply in simple Hindi, commands in English. Everything the tool produces (captions, titles) stays English for US viewers.

## What it is
Python tool in this folder that earns from Whop clipping campaigns (paid per 1K views) and runs the owner's GTA 6 channel. Each campaign is a lane (sources, rules, budget, quota).
Flow: `discovery.py` (Apify listings every 6 h + #campaign-feed → suggested campaigns → `board.py` cards) → `watcher.py` (YouTube/Twitch/folder/Dropbox share links, no Dropbox account) → `moments.py` (Gemini watches YouTube; else transcript + `llm.py` router) → `download.py` → `render.py` (1080x1920, captions) → `metadata.py` → `review.py` (score ≥ 9 auto-approves) → `schedule.py` (US Eastern slots) → `publish.py` (API where keys exist; else video + caption to Telegram/Discord for the owner to post) → `analytics.py`.

## Where things happen (keep it this way)
- Discord (`discord_bot.py`, `commands.py`): all control, alerts, buttons, commands.
- Telegram (`telegram.py`): only delivers clips the owner posts by hand (video + caption): YouTube until the audit, TikTok, X, and Instagram/Facebook if their API fails. No commands.
- Task "Clip Factory" runs `windows\start.ps1`: auto-start, crash restart, installs new GitHub commits (and zips dropped in `Desktop\CLIPING TOOL`) within about 30 seconds; restarts the pipeline within ~10 s when `.env` or `config.local.yaml` changes.
- Discord channels: #general (control), #ai-insights (weekly AI review + daily check `health.py` `!check` + hourly safe auto-fixes `autofix.py` `!fix`: retries ≤2, Instagram/Facebook → Telegram fallback, extra reminder, free-AI error explanations with a report for Claude Code), #campaign-updates (live campaigns with priority tags, kept up to date), #active-campaigns (running lanes), #campaign-feed (followed announcement channels the bot reads).

## Rules
- Never log in to or automate the owner's Whop account.
- Never print or commit `.env`, `secrets\`, tokens.
- Code lives on GitHub: cuze9036/clip-factory (public, no secrets). The laptop installs every new commit on `main` within about a minute (`windows\start.ps1`), replacing this folder's files but keeping keys, data and `config.local.yaml`. So never edit code here without it reaching GitHub: commit and push if this machine can, otherwise send the fix to the owner's main chat. Laptop-only settings go in `config.local.yaml`.
- After code changes: bump `VERSION`, run `python -m pyflakes clipfactory`.
- Read logs with `-Tail 30`, never whole files.

## State (updated 2026-11-22)
Working: Discord (slash commands, buttons), Telegram hand-off, Gemini + FreeLLMAPI, YouTube login, Apify finder, Gmail audit watcher (read-only), GPU (captions + NVENC), auto-update via GitHub, offline alert (healthchecks.io, HEARTBEAT_URL set), Meta keys set (Instagram + Facebook post by themselves). Selftest all OK; it also checks the Instagram/Facebook keys ("Instagram posting", "Facebook posting").
Join flow: #campaign-updates card → Open campaign → join on Whop (owner) → I joined → paste the page → lane starts, card moves to #active-campaigns.
Posting: YouTube = owner posts via Telegram until Google's API audit passes (`always_manual: [youtube]`; audit submitted 2026-11-22). Instagram + Facebook = Meta Graph API once keys are set (IG_MAIN_USER_ID, IG_MAIN_TOKEN, FB_MAIN_PAGE_ID, FB_MAIN_PAGE_TOKEN), else Telegram. TikTok + X = Telegram, owner posts. Hand-posted clips reach Telegram up to 24 h early (with the time in ET and IST); Discord reminds 30 min before the slot (`manual_posting`). Slots per platform are in `schedule.platform_slots` / `manual_slots` (US evening peaks; manual ones fit IST waking hours), ±12 min jitter. X API (credits + keys X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET) is for later; nothing to do now. No screen/remote-desktop automation of any platform.
Campaign guide (`guide.py`, `!guide`): the pasted page and reference material become do/avoid lines in every AI prompt. Each #active-campaigns card has 📋 Guide / 🎬 Footage / ⏸ Pause buttons: the next paste goes to that campaign only; with several campaigns and no id, commands ask which. Google Docs/Slides/Sheets links shared with "anyone with the link" are read directly; long pastes arrive as message.txt and are read too.
Review cards have 🔁 Re-edit (`reedit.py`, `!reedit`): new titles, start earlier/later, longer/shorter, different moment, or a typed note the free AI turns into those changes.
Paid campaigns: #Sponsored (or the campaign's #<Brand>Ad) on the first line of every caption; YouTube paid-promotion flag.
AI router (`llm.py`): text work tries `llm.text_order` (free-llm → gemini → ollama); watching videos uses Gemini only; `!ai` shows usage per provider.
Weekly self-review (`insights.py`, `!insights`): free AI only (Gemini / FreeLLMAPI, never Claude) reads last week's views by platform, slot, moment type, campaign; saves title/moment tips into every AI prompt and skips a clearly weak slot (bounded); everything else is a suggestion on Discord.
Keys go in with `windows\set-key.ps1 <NAME>` (hidden input), never in chat.
Next: join the first campaign from #campaign-updates (owner joins on Whop). Later: X API, TikTok API.

Restart: `powershell -ExecutionPolicy Bypass -File "$([Environment]::GetFolderPath('Desktop'))\Clipping Tools\windows\restart.ps1"` (restarts the pipeline in ~10 s; never starts a second runner)
