# Public-signal monitor

Special Entry Darshan availability (`slot/get_availability`) is only served after OTP login, and this
project does not automate login. Until a green-date capture shows what a non-zero `avl` means, this
monitor watches the **public** signals that come before availability:

- portal CMS: latest updates, daily schedules, sevas, banners, carousels
- legacy booking site (its public `#/loginTimer` page): booking timer (`Timer.json`, `getTimerProperties`) and its latest updates

It alerts when their text changes, and marks changes that mention Special Entry Darshan, SED, 300,
quota, release, slot or online booking as **IMPORTANT** (a quota-release announcement, a
booking-window timer switching on).

## Run (on your own machine)

```bash
cd monitor
pip install -r ../poc/requirements.txt
python -m playwright install chromium
python public_monitor.py --once      # first run records a baseline
python public_monitor.py             # check every 30 min until Ctrl-C
python public_monitor.py --interval 60
```

Alerts print in the terminal (with a bell) and are appended to `monitor/state/alerts.log`. The last
seen text is kept in `monitor/state/public_monitor.json` (public content only; the folder is
git-ignored).

## Alerts on your phone (ntfy)

1. Install the free **ntfy** app (Android: Play Store / F-Droid, iPhone: App Store). No account needed.
2. In the app, tap **+** and subscribe to a topic name that nobody can guess, e.g.
   `ttd-<your-name>-<random letters>`. Anyone who knows the name can read it, so keep it random.
3. On the PC, send a test, then start the monitor with the same topic:

```powershell
python public_monitor.py --ntfy-topic <your-topic> --test-alert
python public_monitor.py --ntfy-topic <your-topic>
```

Or set it once: `setx TTD_NTFY_TOPIC <your-topic>` (open a new terminal afterwards) and run
`python public_monitor.py`. IMPORTANT changes arrive as high-priority notifications. Alert text is
public TTD content only. The PC must stay on and running the monitor.

## Run it in the cloud (GitHub Actions)

`.github/workflows/ttd-monitor.yml` runs one check every 30 minutes on GitHub's servers, so alerts
keep coming when your PC is off.

1. In the repository on GitHub: **Settings → Secrets and variables → Actions → New repository
   secret**. Name `TTD_NTFY_TOPIC`, value your ntfy topic. Never put the topic in a file: this
   repository is public.
2. **Actions → TTD public monitor → Run workflow** to run a check now (tick "Only send a test
   notification" to just test the phone). The first check records the baseline; later checks alert on
   changes.
3. The last seen text is kept between runs in the Actions cache. If the cache expires, the next run
   just records a new baseline.

Notes: GitHub may delay scheduled runs by several minutes at busy times, and pauses schedules in
public repositories after 60 days without any commit (re-enable it on the Actions tab). If TTD stops
answering GitHub's servers, you get one "site unreachable" notification after three failed checks,
and one when it recovers.

## How it behaves

- Each check opens the two official sites (portal home, and the legacy site's public pre-login
  timer page) in a fresh headless Chromium and lets the pages make their
  normal requests; it reads the responses the pages already fetch. It does not call TTD APIs directly,
  log in, click, fill forms, or keep cookies between checks.
- Default interval 30 minutes with ±20% jitter; minimum 10 minutes.
- If a watched response returns 403/429/5xx or nothing loads, the next check waits twice as long
  (up to 4 hours). It never retries quickly.
- Timestamps and IDs (`updatedAt`, `publishedAt`, `id`, ...) are ignored so republishing the same
  text does not alert.
- "Not seen this time" lists sources the pages did not request on that check. If a source is never
  seen it simply isn't monitored.
- Responses that are not JSON (e.g. `getTimerProperties`) are tracked as text, or by hash if long, so
  any change still alerts but the content is not shown.

## Not included yet

Telegram, and anything that reads `get_availability` (login-gated).
