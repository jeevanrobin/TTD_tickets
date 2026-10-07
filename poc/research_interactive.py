"""Record network traffic while you browse the official TTD portal by hand.

Phase 1B research tool. It opens a visible Chromium window on
https://ttdevasthanams.ap.gov.in/ and records page navigations, XHR and fetch
requests until you press Enter in the terminal. You do all clicking yourself.

The script never clicks, types, logs in, enters OTPs, solves CAPTCHAs, books,
pays, or touches security tokens. It does not save cookies or browser storage,
and it applies the same header/PII redaction as record_network.py.

Usage:
    python research_interactive.py
    python research_interactive.py --no-screenshots   # skip screenshots (e.g. if you log in)
    TTD_CHROMIUM_PATH=/path/to/chrome python research_interactive.py

Output (in ./output/interactive_<timestamp>/):
    requests.jsonl     one line per navigation / XHR / fetch, written as it happens
    responses/         JSON bodies from the official TTD hosts, PII-like keys redacted
    screenshots/       one screenshot after each main-page navigation, plus a final one
    summary.txt        navigations, flagged URLs, then every XHR/fetch request
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from record_network import API_TYPES, BASE_URL, MAX_BODY_BYTES, clean_headers, is_sensitive_endpoint, redact, safe_name, sanitize_url

# Official TTD hosts. The booking flow hands off from the portal to tirupatibalaji.ap.gov.in for login.
TTD_HOSTS = (urlsplit(BASE_URL).netloc, "tirupatibalaji.ap.gov.in")
RECORDED_TYPES = API_TYPES | {"document"}
FLAG_TERMS = (
    "availability", "slot", "quota", "darshan", "seva", "booking", "capacity",
    "inventory", "schedule", "timeslot", "calendar", "date",
)
FLAG_RE = re.compile("|".join(FLAG_TERMS), re.IGNORECASE)
SCREENSHOT_DELAY = 2.0  # seconds after a navigation before the screenshot


def is_ttd(url):
    host = urlsplit(url).netloc
    return any(host == h or host.endswith("." + h) for h in TTD_HOSTS)


def flags_for(url):
    """Flag terms in the path or query parameter names of TTD-host URLs only.

    Query values and non-TTD hosts are ignored, so analytics beacons that carry the
    page title or URL in their query string are not flagged.
    """
    if not is_ttd(url):
        return []
    parts = urlsplit(url)
    target = " ".join([parts.path, *parse_qs(parts.query, keep_blank_values=True).keys()])
    return sorted({m.group(0).lower() for m in FLAG_RE.finditer(target)})


def sanitize_post(request):
    raw = request.post_data
    if not raw:
        return None
    if is_sensitive_endpoint(request.url):
        return "<sensitive endpoint, body not saved>"
    try:
        return redact(json.loads(raw))
    except (ValueError, TypeError):
        pass
    ctype = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in ctype:
        return redact(parse_qs(raw))
    return "<non-JSON body omitted>"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Recorder:
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.records = []
        self.navigations = []
        self.failures = []
        self.shutdown_errors = []
        self.seen_endpoints = set()
        self.pending_screenshots = []  # (due_time, page, label)
        self.lock = threading.Lock()
        self.jsonl = (run_dir / "requests.jsonl").open("a", encoding="utf-8")

    def write(self, obj):
        self.jsonl.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.jsonl.flush()

    # --- event handlers -------------------------------------------------------

    def on_response(self, response):
        try:
            request = response.request
            if request.resource_type not in RECORDED_TYPES:
                return
            ctype = response.headers.get("content-type", "")
            parts = urlsplit(request.url)
            record = {
                "time": now(),
                "event": "response",
                "method": request.method,
                "url": sanitize_url(request.url),
                "host": parts.netloc,
                "path": parts.path,
                "query": redact(parse_qs(parts.query)),
                "resource_type": request.resource_type,
                "status": response.status,
                "content_type": ctype,
                "request_headers": clean_headers(request.headers),
                "flags": flags_for(request.url),
                "json_body_captured": False,
            }
            post = sanitize_post(request)
            if post is not None:
                record["post_data"] = post
            if request.resource_type in API_TYPES and "json" in ctype:
                if not is_ttd(request.url):
                    record["body_note"] = "third-party host, body not saved"
                elif is_sensitive_endpoint(request.url):
                    record["body_note"] = "sensitive endpoint (login/OTP/session/user/gatekeeper), body not saved"
                else:
                    try:
                        body = response.body()
                        if len(body) <= MAX_BODY_BYTES:
                            name = safe_name(len(self.records), request.url)
                            data = redact(json.loads(body))
                            (self.run_dir / "responses" / name).write_text(
                                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
                            )
                            record["body_file"] = f"responses/{name}"
                            record["json_body_captured"] = True
                        else:
                            record["body_note"] = f"body larger than {MAX_BODY_BYTES} bytes, not saved"
                    except (PlaywrightError, ValueError) as exc:
                        record["body_note"] = f"body not captured: {type(exc).__name__}"
            self.records.append(record)
            self.write(record)
            if request.resource_type in API_TYPES:
                self.print_if_new(record)
        except Exception as exc:  # one bad response must not lose the rest of the run
            self.failures.append({"method": "?", "url": sanitize_url(getattr(response, "url", "?")), "error": f"recorder error: {exc!r}"})

    def on_failed(self, request):
        if request.resource_type not in RECORDED_TYPES:
            return
        failure = {"time": now(), "method": request.method, "url": sanitize_url(request.url), "error": request.failure}
        self.failures.append(failure)
        self.write({"event": "failed", **failure})

    def on_navigated(self, page, frame):
        if frame != page.main_frame:
            return
        url = frame.url
        nav = {"time": now(), "event": "navigation", "url": sanitize_url(url), "flags": flags_for(url)}
        self.navigations.append(nav)
        self.write(nav)
        print(f"\n== NAV  {nav['url']}")
        if not is_ttd(url) and url != "about:blank":
            print("   !! This page is not on an official TTD host. Recording continues; do not enter personal or payment details.")
        with self.lock:
            label = f"{len(self.navigations):03d}_{urlsplit(url).path.strip('/').replace('/', '_') or 'home'}"
            self.pending_screenshots.append((time.monotonic() + SCREENSHOT_DELAY, page, label))

    def attach(self, page):
        page.on("framenavigated", lambda frame: self.on_navigated(page, frame))

    # --- helpers --------------------------------------------------------------

    def print_if_new(self, r):
        key = (r["method"], r["host"], r["path"])
        if key in self.seen_endpoints:
            return
        self.seen_endpoints.add(key)
        flag = f"   <-- FLAG: {', '.join(r['flags'])}" if r["flags"] else ""
        print(f"   new {r['resource_type']:5} {r['method']:6} {r['status']} {r['url']}{flag}")

    def take_due_screenshots(self, screenshots_dir, force=False):
        with self.lock:
            due = [s for s in self.pending_screenshots if force or s[0] <= time.monotonic()]
            self.pending_screenshots = [s for s in self.pending_screenshots if s not in due]
        for _, page, label in due:
            if page.is_closed():
                continue
            try:
                page.screenshot(path=str(screenshots_dir / f"{label}.png"), full_page=True)
            except PlaywrightError as exc:
                self.failures.append({"method": "-", "url": sanitize_url(page.url), "error": f"screenshot failed: {exc.message.splitlines()[0]}"})


def wait_for_enter(stop):
    try:
        input()
    except EOFError:
        pass
    stop.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="output", help="output directory (default: ./output)")
    parser.add_argument("--no-screenshots", action="store_true", help="do not save screenshots")
    args = parser.parse_args()

    run_dir = Path(args.out) / f"interactive_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    screenshots_dir = run_dir / "screenshots"
    (run_dir / "responses").mkdir(parents=True, exist_ok=True)
    screenshots_dir.mkdir(exist_ok=True)
    rec = Recorder(run_dir)
    if args.no_screenshots:
        rec.take_due_screenshots = lambda *a, **k: rec.pending_screenshots.clear()

    def safe_close(label, closer):
        try:
            closer()
        except Exception as exc:
            rec.shutdown_errors.append(f"{label}: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")

    stop = threading.Event()
    try:
        pw = sync_playwright().start()
        browser = context = None
        try:
            browser = pw.chromium.launch(headless=False, executable_path=os.environ.get("TTD_CHROMIUM_PATH"))
            context = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata", no_viewport=True)
            context.on("response", rec.on_response)
            context.on("requestfailed", rec.on_failed)
            context.on("page", rec.attach)  # also covers tabs/popups the site opens
            page = context.new_page()

            print(f"Opening {BASE_URL}/")
            print("Browse the site by hand. Do not log in, enter OTPs, or book anything you do not intend to.")
            print("Press Enter here to stop recording.\n")
            try:
                page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=60_000)
            except PlaywrightError as exc:
                print(f"Initial load failed: {exc.message.splitlines()[0]}", file=sys.stderr)

            threading.Thread(target=wait_for_enter, args=(stop,), daemon=True).start()
            # Sync Playwright only delivers events while the main thread is inside a Playwright call,
            # so poll with wait_for_timeout instead of blocking on input().
            while not stop.is_set():
                live = [p for p in context.pages if not p.is_closed()]
                if not live:
                    print("Browser window closed; stopping.")
                    break
                live[-1].wait_for_timeout(250)
                rec.take_due_screenshots(screenshots_dir)

            print("Stopping...")
            rec.take_due_screenshots(screenshots_dir, force=True)
            if not args.no_screenshots:
                for i, p in enumerate(p for p in context.pages if not p.is_closed()):
                    try:
                        p.screenshot(path=str(screenshots_dir / f"final_{i}.png"), full_page=True)
                    except PlaywrightError:
                        pass
        finally:
            if context is not None:
                safe_close("context.close", context.close)
            if browser is not None:
                safe_close("browser.close", browser.close)
            safe_close("playwright.stop", pw.stop)
    except KeyboardInterrupt:
        rec.shutdown_errors.append("interrupted with Ctrl-C")
    except Exception as exc:
        rec.shutdown_errors.append(f"run aborted: {type(exc).__name__}: {exc}")
    finally:
        for e in rec.shutdown_errors:
            safe_write(rec, {"event": "shutdown_error", "error": e})
        safe_close_file(rec)
        write_summary(rec)

    return 0 if rec.records else 1


def safe_write(rec, obj):
    try:
        rec.write(obj)
    except Exception:
        pass


def safe_close_file(rec):
    try:
        rec.jsonl.close()
    except Exception:
        pass


def write_summary(rec):
    api = [r for r in rec.records if r["resource_type"] in API_TYPES]
    flagged = [r for r in api if r["flags"]]
    lines = [
        f"Start URL: {BASE_URL}/",
        f"Navigations: {len(rec.navigations)}  XHR/fetch: {len(api)}  Flagged XHR/fetch: {len(flagged)}  "
        f"Failed: {len(rec.failures)}  Shutdown errors: {len(rec.shutdown_errors)}",
        "",
        "Page navigations (main frame):",
        *[f"  {n['time']}  {n['url']}{'  [' + ', '.join(n['flags']) + ']' if n['flags'] else ''}" for n in rec.navigations],
        "",
        f"FLAGGED XHR/fetch (URL contains one of: {', '.join(FLAG_TERMS)}):",
        *[f"  {r['method']} {r['status']} {r['url']}  [{', '.join(r['flags'])}]" for r in flagged],
        "",
        f"Every XHR/fetch request ({len(api)}), in order:",
    ]
    for i, r in enumerate(api, 1):
        captured = f'yes ({r["body_file"]})' if r["json_body_captured"] else f'no{" - " + r["body_note"] if r.get("body_note") else ""}'
        lines += [
            f'  [{i}] {r["time"]}  {r["method"]} {r["status"]} {r["url"]}' + (f'  <-- FLAG: {", ".join(r["flags"])}' if r["flags"] else ""),
            f'      content-type: {r["content_type"] or "-"}',
            f'      query: {json.dumps(r["query"], ensure_ascii=False) if r["query"] else "-"}',
            f'      post body: {json.dumps(r["post_data"], ensure_ascii=False)[:500] if "post_data" in r else "-"}',
            f"      json body captured: {captured}",
        ]
    lines += [
        "",
        "Failed requests:",
        *[f'  {f["method"]} {f["url"]}  ({f["error"]})' for f in rec.failures],
        "",
        "Shutdown errors (output was still written):",
        *[f"  {e}" for e in rec.shutdown_errors],
    ]
    (rec.run_dir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{len(rec.navigations)} navigations, {len(api)} XHR/fetch ({len(flagged)} flagged).")
    print(f"Saved to {rec.run_dir}")


if __name__ == "__main__":
    sys.exit(main())
