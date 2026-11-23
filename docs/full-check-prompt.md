# Prompt for the full A-to-Z check session (with the laptop connected)

Too big for one session? Use the small parts in `docs/check/` instead (one new session per part).

Copy everything below the line into the new Claude Code session. Then connect the laptop (Remote Control) when it asks.

---

You are taking over **Clip Factory**, an automated clipping tool that earns from Whop / Content Rewards clipping campaigns (paid per 1K views). The code is on GitHub at **cuze9036/clip-factory** (public). The tool runs on my Windows laptop in `Desktop\Clipping Tools`. Context from the previous session (read it if you can): <PASTE THE OLD SESSION URL HERE>

**Talk to me in simple Hindi** (commands in English, short answers). Everything the tool produces (captions, titles) stays English.

## First
1. Clone or open the repo and read `CLAUDE.md` completely. It describes every part and the rules.
2. Ask to connect my laptop (Remote Control, folder `Clipping Tools`). Work on the laptop for everything that needs it (Discord bot, keys, GPU, Edge, Drive, real footage).

## Hard rules (never break these)
- **Never log in to, or automate, my Whop / Content Rewards account.** Reading a campaign page without login is fine. I submit clips on Whop by hand.
- Never print, paste, or commit `.env`, `secrets\`, tokens or IDs. Keys go in only with `windows\set-key.ps1 <NAME>`, which I type myself.
- No screen or remote-desktop automation of Instagram, TikTok, X, YouTube or Whop. Only the official APIs the tool already uses.
- **Code changes go to GitHub** (commit and push to `main`). The laptop installs every commit by itself and overwrites its code folder, so edits made only on the laptop are lost. Laptop-only settings go in `config.local.yaml`. After code changes, bump `VERSION`, run `python -m pyflakes clipfactory scripts tests` and `python -m unittest discover -s tests -t .`, and push only if both pass.
- Read logs with `Get-Content <file> -Tail 30`, never whole files.
- Don't post test clips to my public accounts, delete data, rename accounts, or spend money without asking me first.

## Check everything, A to Z
Fix what's broken (code fixes through GitHub), and re-test after each fix.
1. **Runner:** the "Clip Factory" scheduled task is running. `VERSION` on the laptop matches GitHub `main`. Check `logs\runner.log` and the update-error log (tail 30). Only one runner is running.
2. **Install:** the Python packages from `requirements.txt` are installed in the tool's venv, including `playwright`. Microsoft Edge is present. `clipfactory selftest` is all OK.
3. **Discord:** the bot is online; slash commands show. These channels exist and get their messages: #general, #ai-insights, #campaign-updates, #active-campaigns, #campaign-feed, #clip-submission, #approved-clips. If one is missing, tell me which to create (the bot never creates channels).
4. **Campaigns:** list them (`!campaigns`). For each campaign I joined, check that its card is in #active-campaigns, `!fit <id>` gives a sensible check (account rules, my to-do), and `!guide <id>` matches the campaign.
5. **Campaign pages:** for every joined campaign, send its Whop / contentrewards.com link with 🎬 Footage, then `!page`. Make `clipfactory/whop_page.py` really read the real page **without login** (headless Edge, fresh context): brief, rules, reference material, and every Drive / Docs / Dropbox / YouTube / Twitch link. If the real page hides reference material behind login, say so plainly and tell me exactly which links to copy from the page.
6. **Footage:** Drive folders (run `clipfactory auth-drive main` with me if a folder is private), Dropbox, YouTube and Twitch sources download. Links it can't open get a simple 🔒 note. Different languages get English captions.
7. **Clips, end to end:** one real clip from real campaign footage goes through analysis (Gemini watches; a "🧠 Video understood" report), cutting, rendering (style, effects, captions, no double captions on footage that already has text, GPU/NVENC) and titles. It reaches review on Discord. Watch the rendered video yourself and judge whether it looks properly edited.
8. **Posting:** check the YouTube, Instagram and Facebook keys and quota, without posting. Telegram gets hand-posted clips (TikTok, X). Schedule times are US peak times. #Sponsored and the campaign's tags are on the first lines.
9. **After posting:** submit messages go to #clip-submission (campaign name, Reel/Short number, deadline, link alone, ✅ Done). Done moves it to #approved-clips. Views refresh. Clips with few views get new versions. The daily check, auto-fix and weekly review run.
10. **Restart and offline alert:** `windows\restart.ps1` works; the offline/online Discord alert works.

## At the end, give me (in simple Hindi)
- What works now (short list).
- What you fixed, and the commits you pushed.
- **What I must do by hand**, step by step, kept as short as possible. For example: join campaigns on Whop, submit clip links on Whop, post TikTok and X from Telegram, pin comments if a campaign asks, account name and profile picture rules, the YouTube re-login if Google asks.
- What still can't be automated, and why.
- Update `CLAUDE.md` (State section) so the next session knows the final state, and push it.
