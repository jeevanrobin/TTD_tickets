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
  name contains token, auth, session, csrf, xsrf, signature, secret, api-key or gatekeeper, or identifies
  the user or device (user, uid, client-id, customer, account, mobile, phone, email, device; e.g. TTD's
  `userid` header). `user-agent` is kept.
- Request and response bodies of **sensitive endpoints are never saved**: any path containing login,
  logout, otp, session, auth, user, profile, account, payment, captcha or gatekeeper. That covers
  `initiate_login_after_checks`, `session/complete/using_mobileno_n_otp` and `user/client/get_details`.
- Everything else (JSON bodies, query strings, form/JSON POST bodies, and the query part of every URL
  printed or saved) goes through the shared redaction in `record_network.py`: PII-like keys (name,
  mobile, email, Aadhaar, token, OTP, login, contact, ...) become `<redacted>`, and so does any value
  that looks like a mobile number, email address, 12-digit Aadhaar number or JWT, whatever its key.
- Screenshots capture whatever is on screen, including your name or number once logged in. If you log
  in, run with `--no-screenshots` or delete the screenshots afterwards, and do not share them.

## Observed flow (run of 2026-10-07)

From Jeevan's manual runs with `research_interactive.py`:

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

5. Jeevan then **logged in manually** (mobile + OTP typed by hand; the script did not interact). The
   login itself runs on the portal API: `POST /api/sdn/rest/v1/initiate_login_after_checks`, then
   `POST /api/sdn/rest/v1/session/complete/using_mobileno_n_otp`.
6. The browser returns to `ttdevasthanams.ap.gov.in/slot-booking?flow=sed&flowIdentifier=sed`
   (Special Entry Darshan), which calls `GET /api/sdn/rest/v1/user/client/get_details` and then
   **`GET /api/sdn/rest/v1/slot/get_availability`**.

**Confirmed: availability is served by `GET https://ttdevasthanams.ap.gov.in/api/sdn/rest/v1/slot/get_availability`,
and it is only reached after OTP login.** Opening Special Entry Darshan without a session sends you
to the login page; the call happens only on the logged-in `slot-booking` page. (That the API itself
rejects anonymous requests is inferred from the flow, not tested, and should not be tested by
replaying it.)

Observed request: `GET` with **no query string and no body**; the only non-standard request header
is `userid` (the account id, now dropped by the recorder). So the service/date/persons selection is
not sent to this call: it either returns the whole calendar for the flow, or the selection lives in
server-side session state set by earlier calls. It fired twice when the `slot-booking?flow=sed` page
loaded. ### `get_availability` response shape

From the logged-in capture of 2026-10-07 (two responses, identical content). Real structure, with the
per-date list shortened:

```json
{
  "status": "success",
  "result": {
    "20261007": { "avl": 0 },
    "20261008": { "avl": 0 },
    "...": "one key per bookable date, YYYYMMDD",
    "20261218": { "avl": 0 },
    "20261230": { "avl": 0 },
    "20261231": { "avl": 0 },
    "blockedDays": [20261011, 20261015, 20261016, 20261017],
    "enableStats": true
  },
  "response_time": "2026-10-07 12:05:19"
}
```

What the capture shows:

- `result` is a **date-level calendar for Special Entry Darshan**: `YYYYMMDD` → `{ "avl": int }`, from
  today (2026-10-07) to 2026-12-31. The non-date keys `blockedDays` and `enableStats` sit in the same
  object, so a parser has to skip non-digit keys.
- **`blockedDays` are dates (YYYYMMDD ints)**, and they are exactly the four October dates missing from
  the map (Oct 11, 15, 16, 17). Blocked dates are listed there instead of in the calendar.
- **Every `avl` was `0`** for all 71 dates. Special Entry Darshan was fully booked for the whole
  window at capture time, which matches it normally selling out within minutes of each quota release.
  A monitor would watch for any date's `avl` going above 0 (cancellations, or a newly released month).
- `response_time` is the server time in IST (12:05:19 IST = 06:35:19 UTC, the capture time).
- There is **no per-slot or per-persons breakdown** in this response.

Still unknown:

- **What a non-zero `avl` counts** (tickets or slots). Only zeros were seen; needs a capture on a day
  with availability, compared with what the calendar shows.
- **Why Dec 19–29 is absent** although it is not in `blockedDays`: possibly not yet released, or
  handled by a different quota.
- **Where slot data comes from.** In this run the page loaded only `user/client/get_details` and two
  `get_availability` calls; no date was clicked (every date was at 0). The slot call probably follows
  picking a date that has availability.
- **What `enableStats` controls.**

### What a login requirement means for monitoring

The project rules exclude automating login or OTP, and storing cookies or auth headers. With a
login-gated availability API that rules out:

- a headless bot that logs in on a schedule;
- copying a session cookie/token out of the browser and calling `get_availability` from a script
  (that stores and replays auth material, and is likely against TTD's terms);
- keeping a logged-in browser open and auto-refreshing it indefinitely (automated authenticated
  traffic on a personal account, and sessions expire anyway).

What remains possible:

1. **Public-signal notifier (recommended for Phase 2).** Watch unauthenticated sources for changes
   that precede availability: CMS `universal-latest-updates` and `daily-schedules`, the
   `tirupatibalaji` `Timer.json` / `getTimerProperties` booking-window data, and quota-release
   announcements. Notify "booking window opened / new quota announced", then the person logs in and
   checks themselves.
2. **Assisted "check now" session.** The person starts the visible browser, logs in by hand, opens
   Special Entry Darshan, and the tool passively reads the `get_availability` response that page
   loads and summarises it (dates, slots, counts). No stored session, no polling; one check per
   human login. Useful for reading the calendar faster, not for unattended alerts.

Unattended slot-count alerts are not achievable within these rules.

Earlier checks that are still worth doing for option 1:

- the redacted bodies of `getTimerProperties`, `Timer.json` and `serviceAndRequestTypeIds` (they may
  hold release times or service IDs, which is public schedule information, not availability);
- whether any public page on either host shows a quota calendar or "booking open/closed" status
  without login;
- whether `cms/api/daily-schedules` or `universal-latest-updates` announce quota release dates.


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
