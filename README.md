# hosto-uptime

External, **$0** uptime + content watchdog for the Hosto customer sites. Runs on GitHub
Actions (outside pve-2, the VPS, and Heroku), so it survives any single piece of our infra
dying. Alerts go to the existing Telegram group via the Bot API.

Replaces `hosto-infra/scripts/failover-monitor.sh`, which ran **on the VPS** and therefore
died with it in the 2026-08-04 outage. Same alerting semantics (silence == healthy, debounce,
re-alert while down, recovery with downtime duration, loud send failures), but watching every
**public** customer URL from the outside, with a content-keyword option.

## Setup (one-time, ~2 min)

1. Repo is already created from these files.
2. **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `TELEGRAM_BOT_TOKEN` — the alerts bot token
   - `TELEGRAM_CHAT_ID` — the target chat (the `-5362516458` alerts group)
3. **Actions tab** → enable workflows if prompted → run **uptime** once via *Run workflow*
   to confirm it works (it should say "all healthy" or page the currently-down sites).

That's it. It then runs every ~10 min on its own.

## What it does

- `targets.json` — the watch list (name + url, optional `keyword` that must appear in the body).
- Each run probes every target concurrently (3 in-run attempts 3s apart to filter a momentary blip).
- Pages Telegram after `FAIL_THRESHOLD` (default 2) consecutive failed **runs**, re-alerts every
  `RENOTIFY_SECONDS` (default 1800) while down, and sends a recovery notice with the downtime.
- `state.json` is cross-run memory, committed back **only when it changes** → a healthy
  platform makes zero commits.
- A failed Telegram send makes the run exit non-zero → the Action goes **red** (enable "email
  on failed workflow" in your GitHub notification settings for a second, independent alert path).

## Tuning (Actions env, or edit the workflow)

| Var | Default | Meaning |
|---|---|---|
| `FAIL_THRESHOLD` | 2 | consecutive failed runs before the first DOWN alert |
| `RENOTIFY_SECONDS` | 1800 | re-alert cadence while a target stays down |
| `PROBES` / `PROBE_GAP` | 3 / 3 | in-run attempts and the gap between them (blip filter) |
| `HTTP_TIMEOUT` | 10 | per-request timeout |

## Notes

- GitHub cron is best-effort and can lag a few minutes under load; realistic detection is
  ~10–20 min. It also disables scheduled workflows after 60 days of **zero** repo activity —
  the state commits during any incident keep it alive; if the platform is perfectly healthy for
  60 days, push a trivial commit.
- Public repo → unlimited free Actions minutes (there are no secrets in the code, only in
  encrypted Secrets). Customer domains in `targets.json` are already-public info.
