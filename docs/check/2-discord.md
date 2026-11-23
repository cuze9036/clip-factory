# Part 2: Discord and campaigns
Read `docs/check/rules.md` first. Files: `clipfactory/discord_bot.py`, `clipfactory/commands.py` (search, don't read whole).
1. Bot online, slash commands show.
2. Channels exist and get messages: #general, #ai-insights, #campaign-updates, #active-campaigns, #campaign-feed,
   #clip-submission, #approved-clips. Missing ones: tell the owner which to create (the bot never creates channels).
3. `!campaigns`. For each joined campaign: card in #active-campaigns, `!fit <id>` is sensible, `!guide <id>` matches.
Result file: `logs\check\2.txt`.
