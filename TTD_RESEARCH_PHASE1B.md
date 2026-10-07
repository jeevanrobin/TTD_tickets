# TTD Research – Phase 1B: Manual navigation with network recording

Phase 1A ([TTD_RESEARCH.md](TTD_RESEARCH.md)) showed that the home page alone loads
`/api/gatekeeper/verify` and CMS/config APIs, but **no availability API**. Availability is only
requested once a person goes into a booking flow. Phase 1B records that flow while a human drives
the browser.

Tool: [`poc/research_interactive.py`](poc/research_interactive.py)

## Ground rules

- Official TTD hosts only: `https://ttdevasthanams.ap.gov.in/` (portal) and
  `https://tirupatibalaji.ap.gov.in/` (where the booking flow hands off for login). The script warns in
  the terminal if the main page leaves these hosts (for example, to a payment gateway) and never saves
  JSON bodies or flags URLs from other hosts.
- You click; the script only listens. It does not click, type, log in, enter OTPs, solve CAPTCHAs,
  book, pay, decrypt configuration, or reuse/replay gatekeeper tokens.
- No cookies or browser storage are saved (fresh browser profile, nothing persisted). Request
  headers that look like credentials are dropped: `cookie`, `authorization`, and any header whose
  name contains token, auth, session, csrf, xsrf, signature, secret, api-key or gatekeeper. JSON bodies,
  query strings and form/JSON POST bodies go through the same PII-key redaction as
  `record_network.py` (name, mobile, email, Aadhaar, token, OTP, etc. become `<redacted>`).
- Screenshots capture whatever is on screen. If you ever log in, run with `--no-screenshots` or delete
  the screenshots afterwards, and do not share them.

## Observed flow (run of 2026-10-07)

From Jeevan's manual run with `research_interactive.py`, without logging in:

1. `ttdevasthanams.ap.gov.in/` loads the Next.js app: `meta.json`, `darshan/config/app_config.json`
   (application config; its content is encrypted and is not to be decrypted), CSS.
2. `/home/dashboard`: `POST /api/gatekeeper/verify` (200), then Strapi-style CMS calls under
   `/cms/api/...` (`universal-headers`, `universal-sevas`, `daily-schedules`, `universal-latest-updates`,
   banners, footers; `notifications` returns 404). These are content, not availability.
3. Choosing a booking service hands off to **`tirupatibalaji.ap.gov.in`**, an older AngularJS app
   (hash routes, XHR): `GET /common/getAllCountryDetails`, HTML templates, then
   `#/loginTimer` with `POST /common/activeEnv`, `POST /common/getTimerProperties`,
   `GET /content/Timer.json`, `POST /common/serviceAndRequestTypeIds`, `POST /common/isDonorFlag`.
4. It then lands on `#/userLogin` (`GET /content/login.json`): the mobile + OTP login page.

**Finding (inferred, not yet proven):** no date, slot or quota request was seen before the login page,
so Darshan/Seva availability appears to sit **behind the OTP login** on `tirupatibalaji.ap.gov.in`.
Under this project's rules (no login or OTP automation) it cannot be read by an automated monitor.
Still worth checking before concluding:

- the redacted bodies of `getTimerProperties`, `Timer.json` and `serviceAndRequestTypeIds` (they may
  hold release times or service IDs, which is public schedule information, not availability);
- whether any public page on either host shows a quota calendar or "booking open/closed" status
  without login;
- whether `cms/api/daily-schedules` or `universal-latest-updates` announce quota release dates.

If none of those carry availability, the realistic Phase 2 is a notifier for public signals
(release announcements, timer/booking-window changes), not slot counts.

## Procedure

```bash
cd poc
pip install -r requirements.txt
python -m playwright install chromium
python research_interactive.py
```

1. A Chromium window opens on the portal. Let the home page settle (gatekeeper check, CMS calls).
2. Navigate by hand to a ticket product **without logging in**, for example
   Special Entry Darshan (₹300), then Arjitha Seva, then any other service you care about.
3. On each service, do only read actions: open the service page, open the calendar, change month,
   click a date, look at time slots, change the number of persons if the UI allows it before login.
   Pause a few seconds after each action so the requests finish and the terminal shows them.
4. If the site asks you to log in (mobile + OTP) before showing the calendar, **stop there**. Write that down:
   it means availability is behind authentication.
5. Watch the terminal. Each new XHR/fetch endpoint is printed once; URLs containing any of
   `availability, slot, quota, darshan, seva, booking, capacity, inventory, schedule, timeslot,
   calendar, date` in the path or a query parameter name are marked `<-- FLAG`. Only URLs on the
   official TTD hosts are flagged, so analytics beacons (which carry the page title in their query) are not. Each page navigation prints `== NAV`.
6. Press **Enter** in the terminal to stop. Output is written to
   `output/interactive_<timestamp>/` even if the browser crashes on shutdown.
7. Keep a short note of what you clicked and when; the timestamps in `requests.jsonl` and
   `summary.txt` let you line requests up with your actions.

## Output

| File | Contents |
|---|---|
| `requests.jsonl` | One JSON line per navigation, XHR, fetch or document response, written live: time, method, full URL, parsed query, sanitized POST body, status, content type, flags, whether the JSON body was saved |
| `responses/` | Redacted JSON bodies from the official TTD hosts (up to 200 KB each) |
| `screenshots/` | One per main-page navigation (taken about 2 s later) plus `final_*.png` |
| `summary.txt` | Navigations, the flagged XHR/fetch list, then every XHR/fetch with query, POST body and capture status, failures, shutdown errors |

## How to identify the availability API

1. **Start from the flagged list** in `summary.txt`, but don't trust names alone. Encrypted or
   generically named endpoints (e.g. `/api/config`, `/api/v1/data`) can carry availability too,
   and `date` also matches words like `update`.
2. **Correlate with your actions.** The availability call is the one that fires right after you open
   the calendar, change the month, or click a date, and fires again (with different parameters) each time
   you repeat that action. Use the timestamps and the `== NAV` lines.
3. **Look at what changes between repeats.** Compare two calls to the same endpoint: the query or POST
   fields that change with your clicks are the parameters for **date**, **service type**,
   **persons** and **time slot**. Record the parameter names in the Phase 1A tables.
4. **Check the response shape.** Open the matching file in `responses/`. An availability response
   usually contains a list of dates or slots with counts or status values (available / full /
   not released). Compare it with what the page shows (screenshots) to confirm the meaning of each field.
5. **Note the preconditions.** For the chosen endpoint, write down whether it:
   - needed login (look for an earlier login page / 401 / 403),
   - depended on the gatekeeper check (did it only appear after `/api/gatekeeper/verify`?),
   - returned plain JSON or an encrypted blob.
   If the data is only available after login, or only as encrypted content, it is **not** usable for
   an automated monitor within this project's rules. Record that rather than working around it.
6. **Write up** the endpoint, method, parameters, a trimmed redacted response and the preconditions in
   `TTD_RESEARCH.md`. That decides whether Phase 2 (monitoring) is feasible, and how.

## Not in scope yet

No polling, scheduling, monitoring loop, or Telegram/notification integration. Those wait until the
availability source and its access conditions are confirmed.
