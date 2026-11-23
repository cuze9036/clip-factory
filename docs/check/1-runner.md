# Part 1: runner and install
Read `docs/check/rules.md` first.
1. Scheduled task "Clip Factory" is running; only one runner process.
2. `VERSION` here equals GitHub `main` (`git ls-remote` or the raw file). Tail 30 of `logs\runner.log` and the update-error log.
3. Every package in `requirements.txt` is in the tool's venv (incl. `playwright`). Microsoft Edge is installed.
4. `clipfactory selftest` is all OK; fix what fails.
Result file: `logs\check\1.txt`.
