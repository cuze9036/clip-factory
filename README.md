# Clip Factory

Turns finished streams and videos into English vertical shorts for US viewers, for the clipping campaigns you join (GTA 6 or anything else). Every campaign runs as its own lane, with its own sources, rules, budget, daily quota and earnings.

**On Windows:** double-click `INSTALL ON WINDOWS.bat`. It sets up everything in `Desktop\Clipping Tools` and starts Clip Factory by itself at login and whenever the internet connects. `SETUP-STEPS.txt` lists the accounts and keys to collect.

## How it works

1. **Watch.** Every 15 minutes the watcher checks each lane's YouTube channels. When a live stream ends (or a new upload appears), it is added to that lane.
2. **Find moments.** Gemini watches the YouTube VOD directly from its URL, in 30-minute chunks, and returns the funny, crazy and viral moments with exact timestamps and a score. If Gemini is unavailable, the tool downloads only the audio, transcribes it locally, finds loudness spikes and asks any LLM in the router instead.
3. **Download only those seconds.** yt-dlp downloads just each moment's time range, not the whole stream.
4. **Edit.** FFmpeg makes a 1080x1920 clip: facecam on top with gameplay below (or a blurred background), word-by-word captions, the hook text for the first 2.5 seconds, and loudness at -14 LUFS.
5. **Write metadata.** The LLM writes 3 English titles, a description and hashtags. The campaign's credit line and required hashtags are always added. Non-English text is rejected.
6. **Review.** Clips the AI scores 9 or 10 post by themselves. The rest arrive on Discord (and the dashboard) with buttons to pick a title or reject.
7. **Post.** Approved clips get the next free US prime-time slot (Eastern time), within the lane's daily quota, at most 5 posts per account per day and 2 hours apart. YouTube is uploaded right away with a scheduled publish time; Instagram and Facebook post 90 to 120 minutes later.
8. **Track.** Views, likes and US viewer share update every 6 hours. Each lane shows its estimated earnings from the campaign's rate, minimum views and per-clip cap, plus which posts still need to be submitted on the campaign page.

## Setup

Needs Python 3.10+, FFmpeg, and [Deno](https://deno.com) (recent yt-dlp versions use it for YouTube).

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env        # then fill in your keys
```

Keys you need, in `.env`:

| Key | Where to get it | Used for |
| --- | --- | --- |
| `YOUTUBE_API_KEY` | Google Cloud console, YouTube Data API v3 | Finding ended streams, reading views |
| `GEMINI_API_KEY` | Google AI Studio | Watching streams, titles |
| `FREE_LLM_BASE_URL`, `FREE_LLM_API_KEY`, `FREE_LLM_MODEL` | Your free OpenAI-compatible LLM API | Backup for titles and moments |
| `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_OWNER_ID` | Discord Developer Portal (see below) | Alerts, clip review, commands |
| `APIFY_TOKEN` | apify.com, Settings → Integrations | Finding new campaigns |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | @BotFather on Telegram | Optional second review channel |
| `IG_MAIN_USER_ID`, `IG_MAIN_TOKEN` | Meta developer app, Instagram Professional account | Posting Reels |
| `FB_MAIN_PAGE_ID`, `FB_MAIN_PAGE_TOKEN` | Meta developer app, Facebook Page | Posting Facebook Reels |

For YouTube uploads, download an OAuth client file (Desktop app) from Google Cloud to `secrets/client_secret.json`, then run:

```bash
.venv/bin/clipfactory auth-youtube main
```

Until Google approves your API project in its audit, uploads made through the API stay private.

## Add campaigns

Clipping platforms like Whop list campaigns where a creator pays per 1,000 views on clips of their content. Each campaign you join becomes its own lane, about any topic: streams, podcasts, any game.

**On the dashboard (easiest):** Campaigns → Add a campaign. Copy all the text on the campaign page, paste it, and click "Fill the form with AI". The AI reads the rate, budget, minimum views, platforms, hashtags, credit line, clip length, source links and rules. Check the form and save.

Sources can be:

| Source | Example | What happens |
| --- | --- | --- |
| YouTube channel | `https://www.youtube.com/@creator` | Each finished stream or new upload is clipped. Gemini watches it directly. |
| Twitch channel | `https://www.twitch.tv/creator` | Each finished VOD is clipped through the transcript route. |
| Folder | `~/Google Drive/Campaign raw files` | Each new file is clipped once it stops syncing. Use this when a campaign shares raw files. |
| Single video | any YouTube, Twitch or Kick video link | Clipped once. |

**Or in `campaigns.yaml`:** add an entry (see the two templates in the file), then run `.venv/bin/clipfactory sync`. If you later edit that campaign on the dashboard, the dashboard version wins.

The tool posts to YouTube, Instagram and Facebook. If a campaign also accepts TikTok or X, those posts are up to you.

## Discord: alerts and control

Everything the tool needs from you arrives in one Discord channel, and you answer there.

| Message | What you do |
| --- | --- |
| New campaign found (rate, budget, platforms, why it fits) | Join it on Whop, then tap **Activate**, or tap **Ignore** |
| Clip ready (small preview, score, 3 titles) | Tap **Post title 1/2/3** or **Reject** |
| Auto-posting (clips scored 9 or 10) | Nothing, or tap **Cancel** before it goes live |
| Live now, submit within 30 minutes | Paste the link on the campaign page, tap **Submitted** |

Commands you can type: `!help`, `!status`, `!review`, `!approve <clip> [title]`, `!reject <clip>`, `!cancel <clip>`, `!todo`, `!submitted <post>`, `!campaign <paste a campaign page>`, `!campaigns`, `!activate <id>`, `!ignore <id>`, `!pause <id>`, `!resume <id>`, `!add <video link> <campaign id>`, `!find`.

Set it up once:

1. Create a private Discord server (or use yours) and a channel such as `#clip-factory`.
2. On the [Discord Developer Portal](https://discord.com/developers/applications), create an application, open **Bot**, copy the token, and turn on **Message Content Intent**.
3. Under **OAuth2 → URL Generator**, tick `bot`, then the permissions Send Messages, Attach Files, Read Message History and View Channels. Open the link and add the bot to your server.
4. In Discord settings, turn on **Developer Mode**. Right-click the channel → Copy Channel ID, and right-click your name → Copy User ID.
5. Put `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` and `DISCORD_OWNER_ID` in `.env`. Only you, in that channel, can control the bot.

## Campaign finder

Every 6 hours the finder reads Whop's public campaign listings, the AI fills in each campaign's terms, and the ones that fit your minimums (`discovery` in `config.yaml`) are sent to Discord. Listings come from an [Apify](https://apify.com/) scraper run with your own `APIFY_TOKEN` (Apify's free plan includes monthly credit). You can also paste any campaign with `!campaign` in Discord or on the dashboard.

The tool never logs in to Whop. Whop has no official API for joining campaigns or submitting clips, and a bot clicking inside your account could get it banned with payouts held. So joining and submitting stay one tap each for you.

## Auto-posting

Clips the AI scores 9 or 10 post without waiting for you. Change `review.auto_approve_score` in `config.yaml` (11 = review everything), or set `auto_approve_score` on a single campaign.

## Run

```bash
.venv/bin/clipfactory run            # everything, forever
.venv/bin/clipfactory dashboard      # http://127.0.0.1:8787
```

Other commands:

```bash
.venv/bin/clipfactory status                                   # numbers for every lane
.venv/bin/clipfactory add-video "https://www.youtube.com/watch?v=..." --campaign streamer-gta6
.venv/bin/clipfactory clip-local recording.mp4 --campaign own-gameplay   # clip a local file right now
.venv/bin/clipfactory demo                                     # demo dashboard on port 8788
```

## Limits

- Gemini reads public YouTube videos only. Long streams are watched in chunks, and the free tier limits how much video it can watch per day, so very long streams may take more than one day or fall back to the transcript route.
- The default YouTube API quota allows about 6 uploads a day per Google Cloud project.
- Whop has no public API for submitting clips, so the dashboard lists each live post that still needs to be submitted on its campaign page.
- Only clip channels whose campaign or clipper program allows it. Copyright strikes can delete a channel.
