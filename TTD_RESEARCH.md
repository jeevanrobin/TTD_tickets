# TTD Ticket Availability – Phase 1 Research

Status: **incomplete – live inspection could not run from the research environment.**
Date: 2026-10-07

## TL;DR

- The research ran in a cloud sandbox whose outbound network policy refuses connections to
  `ttdevasthanams.ap.gov.in` (and `tirupatibalaji.ap.gov.in`, `www.tirumala.org`). Both `curl` and
  Playwright/Chromium failed at the proxy with `CONNECT 403` / `net::ERR_TUNNEL_CONNECTION_FAILED`.
  This is a restriction of the sandbox, not something the TTD site did, and no attempt was made to
  route around it.
- What *could* be confirmed from a static (non-JavaScript) fetch is listed under "Verified".
  Everything about XHR endpoints, parameters and response shapes is **not yet observed** and the
  sections below are left as a structured checklist to fill in from one run of
  [`poc/record_network.py`](poc/record_network.py) on a normal machine.
- Sections marked *(unverified)* are background knowledge about the portal, not observations from
  this session. Treat them as hypotheses to confirm.

## Verified (static fetch of the home page, no JavaScript executed)

| Item | Finding |
|---|---|
| Official booking URL | `https://ttdevasthanams.ap.gov.in/` (canonical link, title "Tirumala Tirupati Devasthanams(Official Booking Portal)") |
| Rendering | Client-side single-page app: the HTML shell contains only "You need to enable JavaScript to run this app". Availability is therefore **not** in the initial HTML; any slot data must arrive via XHR/fetch after the app boots. |
| Analytics | Google Tag Manager `GTM-NQPPL9G` |
| `robots.txt` | `User-agent: *` with `Disallow: /receipt`, `/admin`, `/cms`. Booking/availability routes are not disallowed, but robots.txt is not permission to automate. |
| `/asset-manifest.json` | 404 (so not a default Create-React-App build layout; framework not identified) |

## Booking flow and pages *(unverified – confirm with the PoC)*

Based on how the portal has worked publicly:

1. Home page → choose a service (Special Entry Darshan ₹300, Arjitha Seva, accommodation, etc.).
2. **Login with mobile number + OTP** is generally required before the slot calendar is shown.
   If that is still true, availability is behind authentication and this project must not automate
   past it (see Risks).
3. Calendar of dates with availability colouring → select date → time slots with remaining counts →
   number of persons → pilgrim details → payment. This project stops before step 2 or, at most,
   only reads what is visible without logging in.

Pages involved: to be filled from `requests.jsonl` (`resource_type = document`) and the SPA routes
seen in the address bar.

## Network requests observed

None yet (blocked). After a local run, copy from `output/<ts>/summary.txt`:

| Method | Host + path | Status | Purpose (guess) | Needs auth? |
|---|---|---|---|---|
| | | | | |

## Relevant parameters

To be filled from `query` / `post_data` in `requests.jsonl`:

| Concept | Parameter name | Example value | Where (query/body/header) |
|---|---|---|---|
| Booking/service type | | | |
| Date | | | |
| Number of persons | | | |
| Time slot | | | |

## Response structure

To be filled from `output/<ts>/responses/*.json` (the PoC already replaces PII-like keys such as
name, mobile, email, Aadhaar, token, OTP with `<redacted>`). Paste a trimmed example here.

## Can Playwright monitor availability?

- **Technically:** yes for anything the SPA shows to an anonymous visitor. The page is JavaScript-
  rendered, so plain `requests`/`curl` will not see slot data; a real browser (Playwright) or a
  direct call to a public JSON endpoint the SPA itself uses would.
- **Open question that decides the design:** is the availability calendar visible **without
  logging in**? If it requires OTP login, an automated monitor cannot read it within this
  project's rules (no OTP handling), and the bot should be limited to public signals
  (e.g. quota release announcements, a "booking open/closed" banner).

## Recommended implementation approach

1. Run `poc/record_network.py` once locally (and once with `--path` on the public Darshan page if
   one exists without login) and fill in the tables above.
2. If a public, unauthenticated JSON endpoint returns availability:
   - Poll it at a conservative interval (e.g. every 10–15 minutes, with jitter), one request per
     check, identify the client honestly, stop on any 4xx/429 or challenge page.
   - Prefer reading it through Playwright loading the page normally over hand-crafted API calls, so
     the bot behaves like a visitor and breaks loudly if the site changes.
3. If availability needs login: do not automate it. Monitor only public pages/announcements and
   notify the user to check manually.
4. Notifications (Telegram/email/push) and state diffing ("newly available" only) belong in phase 2.

## Risks

**Technical**
- SPA internals and API paths can change without notice; selectors and endpoints will break.
- Likely WAF/bot protection on a high-traffic government portal; automated traffic may be blocked.
  The bot must back off, never retry aggressively, and never attempt to evade it.
- Cloud/datacenter IPs may be refused outright (as seen here, at least by the sandbox's own policy);
  running from a residential connection may be the only practical host.

**Legal / policy**
- Check the portal's terms of use before automating; government sites in India may prohibit
  automated access, and misuse can fall under the IT Act 2000.
- TTD has publicly acted against touts and bulk booking; a tool that looks like scalping
  infrastructure is high-risk even if it only notifies. Keep it personal-use, low-frequency, and
  read-only.
- Do not store pilgrim personal data; the PoC strips cookies, auth headers and PII-like fields.

**Operational**
- Quotas are released at announced times and sell out in minutes; a notifier helps only if polling
  is close to release time, which conflicts with being gentle on the server. Scheduling checks around
  announced release windows is the better trade-off.
- Login sessions/OTPs expire; anything depending on them would need a human each time.

## Proof-of-concept

[`poc/record_network.py`](poc/record_network.py) opens one public page of the portal in Chromium,
waits for it to settle, and writes every request (method, URL, query, type, status, content-type),
redacted JSON XHR/fetch bodies, a screenshot, and a grouped endpoint summary. It never clicks, logs
in, fills forms, or loops. It was tested in the sandbox against a local test page (fetch capture
and PII redaction work) and against the TTD URL (fails cleanly with the proxy denial recorded).

```bash
cd poc
pip install -r requirements.txt
python -m playwright install chromium
python record_network.py            # home page
python record_network.py --headed   # watch it
```
