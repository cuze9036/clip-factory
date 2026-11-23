# Part 3: campaign pages
Read `docs/check/rules.md` first. File: `clipfactory/whop_page.py`.
1. For each joined campaign (ids in `logs\check\2.txt`), send its Whop / contentrewards.com link with 🎬 Footage, then `!page`.
2. The page must be read WITHOUT login (headless Edge, fresh context): brief, rules, reference material, every
   Drive / Docs / Dropbox / YouTube / Twitch link, followed deep. Fix the code if it misses things.
3. If the real page hides material behind login, say so plainly and list exactly which links the owner must copy.
Result file: `logs\check\3.txt`.
