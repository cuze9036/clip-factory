# Clip Factory

Owner writes Hindi/Hinglish: reply in simple Hindi, commands in English. Everything the tool produces (captions, titles) stays English for US viewers.

## What it is
Python tool in this folder that earns from Whop clipping campaigns (paid per 1K views) and runs the owner's GTA 6 channel. Each campaign is a lane (sources, rules, budget, quota).
Flow: `discovery.py` (Apify listings → suggested campaigns) → `watcher.py` (YouTube/Twitch/folder) → `moments.py` (Gemini watches YouTube; else transcript + `llm.py` router) → `download.py` → `render.py` (1080x1920, captions) → `metadata.py` → `review.py` (score ≥ 9 auto-approves) → `schedule.py` (US Eastern slots) → `publish.py` (YouTube auto; Instagram + Facebook manual) → `analytics.py`.

## Where things happen (keep it this way)
- Discord (`discord_bot.py`, `commands.py`): all control, alerts, buttons, commands.
- Telegram (`telegram.py`): only delivers Instagram/Facebook reels (video + caption). No commands.
- Task "Clip Factory" runs `windows\start.ps1`: auto-start, crash restart, installs new GitHub commits (and zips dropped in `Desktop\CLIPING TOOL`) within about a minute.

## Rules
- Never log in to or automate the owner's Whop account.
- Never print or commit `.env`, `secrets\`, tokens.
- Code lives on GitHub: cuze9036/clip-factory (public, no secrets). The laptop installs every new commit on `main` within about a minute (`windows\start.ps1`), replacing this folder's files but keeping keys, data and `config.local.yaml`. So never edit code here without it reaching GitHub: commit and push if this machine can, otherwise send the fix to the owner's main chat. Laptop-only settings go in `config.local.yaml`.
- After code changes: bump `VERSION`, run `python -m pyflakes clipfactory`.
- Read logs with `-Tail 30`, never whole files.

## State
Set up: Discord, Telegram, Gemini, FreeLLMAPI (`auto:default`), YouTube (OAuth app In production since 2026-11-21, homepage + privacy policy on GitHub Pages `cuze9036.github.io/clip-factory`; uploads stay private until Google's YouTube API audit), Apify free plan (no campaign links).
Selftest: 12/12 OK (2026-11-21, Gemini model `gemini-3.5-flash`).
To do: real clip test with `!add <short GTA video> own-gta6`; first campaign (`!suggested` → join on Whop → `!source 1 <link>` → `!activate 1`); re-run `clipfactory auth-youtube main` once after publishing (Testing-mode tokens expire after 7 days); YouTube API audit form.

Restart: `Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "clipfactory" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Start-ScheduledTask -TaskName "Clip Factory"`
