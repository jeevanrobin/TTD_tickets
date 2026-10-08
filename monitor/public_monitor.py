"""Watch TTD's public (no-login) pages for announcement and booking-window changes.

Special Entry Darshan availability itself is behind OTP login, which this project
does not automate. This monitor watches the public signals that come before
availability instead: announcements, schedules and the booking-window timer.

Each check opens the official sites in a fresh headless Chromium, exactly as a
visitor would, lets the pages make their own requests, and reads these responses:

    ttdevasthanams.ap.gov.in   /cms/api/universal-latest-updates, daily-schedules,
                               universal-sevas, universal-banners, universal-carousels
    tirupatibalaji.ap.gov.in   /content/Timer.json, /content/getLatestUpdates.json,
                               /common/getTimerProperties

It compares the text in them with the previous check and prints what was added
or removed. Text mentioning Special Entry Darshan, quota or release is marked
IMPORTANT. It never logs in, clicks, fills forms, calls APIs directly, or saves
cookies.

Usage:
    python public_monitor.py --once          # one check (the first run just records a baseline)
    python public_monitor.py                 # check every 30 minutes until Ctrl-C
    python public_monitor.py --interval 60   # every 60 minutes (minimum 5)
    python public_monitor.py --ntfy-topic <your-topic>   # also send alerts to your phone (ntfy app)
"""

import argparse
import hashlib
import html
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PORTAL = "https://ttdevasthanams.ap.gov.in/"
# The public pre-login page of the legacy booking site; it loads the booking-window timer.
LEGACY = "https://tirupatibalaji.ap.gov.in/#/loginTimer"

# (label, host, path) of the public responses to watch.
WATCHED = [
    ("Latest updates", "ttdevasthanams.ap.gov.in", "/cms/api/universal-latest-updates"),
    ("Daily schedules", "ttdevasthanams.ap.gov.in", "/cms/api/daily-schedules"),
    ("Sevas", "ttdevasthanams.ap.gov.in", "/cms/api/universal-sevas"),
    ("Banners", "ttdevasthanams.ap.gov.in", "/cms/api/universal-banners"),
    ("Carousels", "ttdevasthanams.ap.gov.in", "/cms/api/universal-carousels"),
    ("Booking timer", "tirupatibalaji.ap.gov.in", "/content/Timer.json"),
    ("Legacy latest updates", "tirupatibalaji.ap.gov.in", "/content/getLatestUpdates.json"),
    ("Timer properties", "tirupatibalaji.ap.gov.in", "/common/getTimerProperties"),
]

IMPORTANT = re.compile(
    r"(special\s*entry|\bsed\b|300|quota|release|online\s*(booking|ticket)|darshan\s*ticket|slot|booking\s*(open|start))",
    re.IGNORECASE,
)
# Keys whose values change on every publish and would cause false alerts.
VOLATILE_KEY = re.compile(r"(createdat|updatedat|publishedat|timestamp|response_?time|servertime|^id$|hash|etag)", re.IGNORECASE)
TAG = re.compile(r"<[^>]+>")
MIN_INTERVAL = 5
MAX_BACKOFF_MINUTES = 240
UNREACHABLE_AFTER = 3
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state" / "public_monitor.json"
ALERT_LOG = HERE / "state" / "alerts.log"


def texts(value, key=""):
    """Collect normalised text leaves from a JSON value, skipping volatile keys."""
    if key and VOLATILE_KEY.search(key):
        return set()
    if isinstance(value, dict):
        out = set()
        for k, v in value.items():
            out |= texts(v, str(k))
        return out
    if isinstance(value, list):
        out = set()
        for v in value:
            out |= texts(v, key)
        return out
    if isinstance(value, bool) or value is None:
        return {f"{key}={value}"} if key else set()
    s = re.sub(r"\s+", " ", TAG.sub(" ", html.unescape(str(value)))).strip()
    if not s:
        return set()
    # Short values are only meaningful with their key (e.g. "enabled=1", "startTime=10:00").
    return {f"{key}={s}" if key and len(s) < 40 else s}


def watched_label(url):
    parts = urlsplit(url)
    for label, host, path in WATCHED:
        if parts.netloc == host and parts.path == path:
            return label
    return None


def check(headed=False):
    """Open both public sites once and return ({label: set_of_texts}, problems)."""
    snapshot, problems = {}, []

    def on_response(response):
        label = watched_label(response.url)
        if not label:
            return
        if response.status in (403, 429) or response.status >= 500:
            problems.append(f"{label}: HTTP {response.status}")
            return
        try:
            body = response.text()
        except PlaywrightError:
            problems.append(f"{label}: body could not be read")
            return
        try:
            snapshot[label] = sorted(texts(json.loads(body)))
        except ValueError:
            # Not JSON (plain text or an encoded blob): track it as one value, by hash if it is long.
            body = body.strip()
            snapshot[label] = [body if len(body) <= 500 else f"<{len(body)} chars, sha256 {hashlib.sha256(body.encode()).hexdigest()[:16]}>"]

    pw = sync_playwright().start()
    browser = None
    try:
        browser = pw.chromium.launch(headless=not headed, executable_path=os.environ.get("TTD_CHROMIUM_PATH"))
        context = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata")
        context.on("response", on_response)
        page = context.new_page()
        for url in (PORTAL, LEGACY):
            try:
                page.goto(url, wait_until="networkidle", timeout=60_000)
                page.wait_for_timeout(8_000)
            except PlaywrightError as exc:
                problems.append(f"{url}: {exc.message.splitlines()[0]}")
    except PlaywrightError as exc:
        problems.append(f"browser: {exc.message.splitlines()[0]}")
    finally:
        for closer in ((browser.close,) if browser else ()) + (pw.stop,):
            try:
                closer()
            except Exception:
                pass
    return snapshot, problems


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


# Phone notifications via ntfy (https://ntfy.sh): set TTD_NTFY_TOPIC or pass --ntfy-topic.
NTFY_SERVER = os.environ.get("TTD_NTFY_SERVER", "https://ntfy.sh")
ntfy_topic = os.environ.get("TTD_NTFY_TOPIC", "")


def push(title, message, important=False):
    """Send a phone notification through ntfy. Failures are printed, never raised."""
    if not ntfy_topic:
        return
    req = urllib.request.Request(
        f"{NTFY_SERVER.rstrip('/')}/{ntfy_topic}",
        data=message[:3500].encode("utf-8"),
        headers={"Title": title, "Priority": "high" if important else "default", "Tags": "warning" if important else "bell"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except (urllib.error.URLError, OSError) as exc:
        print(f"Phone notification failed: {exc}")


def alert(lines):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    block = "\n".join([f"[{stamp}]", *lines, ""])
    print("\a" + block)  # terminal bell
    ALERT_LOG.parent.mkdir(exist_ok=True)
    with ALERT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(block + "\n")
    important = any(line.startswith("IMPORTANT") for line in lines)
    push("TTD: IMPORTANT update" if important else "TTD: site update", "\n".join(lines), important)


def short(s, n=300):
    return s if len(s) <= n else s[: n - 1] + "…"


def run_once(headed=False):
    """Run one check. Returns True if the sites answered normally."""
    stamp = datetime.now().strftime("%H:%M:%S")
    snapshot, problems = check(headed)
    state = load_state()
    previous = state.get("snapshot", {})

    if not previous:
        print(f"[{stamp}] Baseline recorded for {len(snapshot)} of {len(WATCHED)} sources.")
    else:
        lines = []
        for label, current in snapshot.items():
            if label not in previous:
                lines.append(f"{label}: now available")
                continue
            old, new = set(previous[label]), set(current)
            added, removed = sorted(new - old), sorted(old - new)
            if not added and not removed:
                continue
            flag = "IMPORTANT " if any(IMPORTANT.search(s) for s in added) else ""
            lines.append(f"{flag}{label}: {len(added)} added, {len(removed)} removed")
            lines += [f"  + {short(s)}" for s in added[:15]]
            lines += [f"  - {short(s)}" for s in removed[:5]]
        if lines:
            alert(lines)
        else:
            print(f"[{stamp}] No change ({len(snapshot)} sources checked).")

    missing = [label for label, _, _ in WATCHED if label not in snapshot]
    if missing:
        print(f"[{stamp}] Not seen this time: {', '.join(missing)}")
    for p in problems:
        print(f"[{stamp}] Problem: {p}")

    blocked = any("HTTP 403" in p or "HTTP 429" in p for p in problems)
    ok = bool(snapshot) and not blocked

    # Tell the phone once when the sites stop answering (3 failed checks in a row), and once when they recover.
    failures = 0 if ok else state.get("failures", 0) + 1
    if failures == UNREACHABLE_AFTER:
        push("TTD monitor: site unreachable", f"{failures} checks in a row failed. Last problems:\n" + "\n".join(problems[:5]))
    elif ok and state.get("failures", 0) >= UNREACHABLE_AFTER:
        push("TTD monitor: back to normal", "The TTD sites are answering again.")

    # Keep the last good text for sources that were not seen this time.
    merged = {**previous, **snapshot}
    digest = hashlib.sha256(json.dumps(merged, sort_keys=True).encode()).hexdigest()[:12]
    # After a failure, wait 5, 10, 20 ... minutes (up to 4 hours) before the next scheduled check.
    backoff = min(MIN_INTERVAL * 2 ** (failures - 1), MAX_BACKOFF_MINUTES) if failures else 0
    save_state({
        "snapshot": merged,
        "last_check": datetime.now().isoformat(timespec="seconds"),
        "digest": digest,
        "failures": failures,
        "next_check_after": (datetime.now() + timedelta(minutes=backoff)).isoformat(timespec="seconds") if backoff else None,
    })
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    parser.add_argument("--interval", type=float, default=30, help=f"minutes between checks (default 30, minimum {MIN_INTERVAL})")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--ntfy-topic", help="ntfy topic for phone alerts (or set TTD_NTFY_TOPIC)")
    parser.add_argument("--test-alert", action="store_true", help="send a test phone notification and exit")
    args = parser.parse_args()

    global ntfy_topic
    ntfy_topic = args.ntfy_topic or ntfy_topic
    if args.test_alert:
        if not ntfy_topic:
            print("Set TTD_NTFY_TOPIC or pass --ntfy-topic first.")
            return 1
        push("TTD monitor test", "Phone alerts are working.")
        print("Test notification sent.")
        return 0
    if ntfy_topic:
        print("Phone alerts on.")

    if args.once:
        # Scheduled runs (e.g. GitHub Actions) back off the same way as the loop: after failures,
        # skip runs until the waiting time has passed instead of hitting the site on every schedule.
        wait_until = load_state().get("next_check_after")
        if wait_until and datetime.now() < datetime.fromisoformat(wait_until):
            print(f"Backing off after failed checks; skipping until {wait_until}.")
            return 0
        return 0 if run_once(args.headed) else 1

    interval = max(args.interval, MIN_INTERVAL)
    delay = interval
    print(f"Checking every {interval:g} minutes (with jitter). Ctrl-C to stop. Alerts also go to {ALERT_LOG}")
    try:
        while True:
            ok = run_once(args.headed)
            # Back off when the site is refusing or failing; never retry aggressively.
            delay = interval if ok else min(delay * 2, MAX_BACKOFF_MINUTES)
            if not ok:
                print(f"Site did not answer normally; next check in {delay:g} minutes.")
            time.sleep(delay * 60 * random.uniform(0.8, 1.2))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
