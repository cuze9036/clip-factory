# Rules for every check part (keep context small)

- Reply in simple Hindi, short. Commands in English. Tool output (captions, titles) stays English.
- Do ONLY your part. Read only the files your part names; use search (Grep) instead of opening whole files.
- Logs: `Get-Content <file> -Tail 30`. Long command output: pipe to `Select-Object -First 30`.
- Never log in to or automate Whop / Content Rewards. Reading a public page without login is fine.
- Never print or commit `.env`, `secrets\`, tokens or IDs. Keys only via `windows\set-key.ps1 <NAME>`, typed by the owner.
- No screen/remote-desktop automation of Instagram, TikTok, X, YouTube or Whop.
- Never delete files in this folder. Ask before posting to public accounts, renaming accounts or spending money.
- Code fixes: edit, bump `VERSION`, run `python -m pyflakes clipfactory scripts tests` and
  `python -m unittest discover -s tests -t .`, commit and push to GitHub `main` only if both pass
  (the laptop installs commits by itself; local-only edits get overwritten). Laptop-only settings go in `config.local.yaml`.
- When done, write at most 15 lines to `logs\check\<part>.txt` (works / fixed + commit / owner must do / can't automate).
  Create the folder if missing. Then tell the owner in 3-5 Hindi lines and stop.
