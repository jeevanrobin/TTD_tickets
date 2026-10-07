"""Open the official TTD booking portal once and record its public network traffic.

Research proof-of-concept only. It loads one page, waits for it to settle, and
writes what the browser requested. It does not click, log in, fill forms,
solve CAPTCHAs, enter OTPs, book, pay, or loop.

Usage:
    pip install -r requirements.txt
    python -m playwright install chromium   # skip if a Chromium is already available
    python record_network.py                       # home page
    python record_network.py --path /some/route    # another public route, still read-only
    python record_network.py --headed              # watch the browser
    TTD_CHROMIUM_PATH=/path/to/chrome python record_network.py   # use an existing Chromium

Output (in ./output/<timestamp>/):
    requests.jsonl   one line per request: method, url, type, status, content-type
    responses/       bodies of JSON XHR/fetch responses, PII-like keys redacted
    summary.txt      XHR/fetch endpoints grouped, then every XHR/fetch request with method,
                     status, content-type, query params and whether its JSON body was saved
    page.png         full-page screenshot
"""

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

BASE_URL = "https://ttdevasthanams.ap.gov.in"
API_TYPES = {"xhr", "fetch"}
MAX_BODY_BYTES = 200_000

# Never persist credentials or session material.
DROP_HEADERS = {"cookie", "set-cookie", "authorization", "x-auth-token", "x-csrf-token", "x-xsrf-token"}
# Keys whose values are replaced before a response body is written to disk.
PII_KEY = re.compile(
    r"(name|mobile|phone|email|mail|aadhaar|aadhar|idproof|id_number|pan|passport|address|dob|"
    r"birth|gender|age|token|otp|password|session|user)",
    re.IGNORECASE,
)


def redact(value):
    if isinstance(value, dict):
        return {k: ("<redacted>" if PII_KEY.search(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def clean_headers(headers):
    return {k: v for k, v in headers.items() if k.lower() not in DROP_HEADERS}


def safe_name(index, url):
    parts = urlsplit(url)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", parts.netloc + parts.path).strip("_")[:120]
    return f"{index:04d}_{slug}.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", default="/", help="public route on the portal to open (default: /)")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--wait", type=float, default=10.0, help="extra seconds to wait after load (default: 10)")
    parser.add_argument("--out", default="output", help="output directory (default: ./output)")
    args = parser.parse_args()

    url = BASE_URL + (args.path if args.path.startswith("/") else "/" + args.path)
    run_dir = Path(args.out) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (run_dir / "responses").mkdir(parents=True, exist_ok=True)

    records = []
    failures = []
    shutdown_errors = []

    def on_response(response):
        try:
            request = response.request
            ctype = response.headers.get("content-type", "")
            parts = urlsplit(request.url)
            record = {
                "method": request.method,
                "url": request.url,
                "host": parts.netloc,
                "path": parts.path,
                "query": redact(parse_qs(parts.query)),
                "resource_type": request.resource_type,
                "status": response.status,
                "content_type": ctype,
                "request_headers": clean_headers(request.headers),
                "json_body_captured": False,
            }
            if request.post_data:
                try:
                    record["post_data"] = redact(json.loads(request.post_data))
                except (ValueError, TypeError):
                    record["post_data"] = "<non-JSON body omitted>"
            if request.resource_type in API_TYPES and "json" in ctype:
                try:
                    body = response.body()
                    if len(body) <= MAX_BODY_BYTES:
                        name = safe_name(len(records), request.url)
                        data = redact(json.loads(body))
                        (run_dir / "responses" / name).write_text(json.dumps(data, indent=2, ensure_ascii=False))
                        record["body_file"] = f"responses/{name}"
                        record["json_body_captured"] = True
                    else:
                        record["body_note"] = f"body larger than {MAX_BODY_BYTES} bytes, not saved"
                except (PlaywrightError, ValueError) as exc:
                    record["body_note"] = f"body not captured: {type(exc).__name__}"
            records.append(record)
        except Exception as exc:  # one bad response must not lose the rest of the run
            failures.append({"method": "?", "url": getattr(response, "url", "?"), "error": f"recorder error: {exc!r}"})

    def on_failed(request):
        failures.append({"method": request.method, "url": request.url, "error": request.failure})

    def safe_close(label, closer):
        """Close a Playwright object, recording (not raising) any error."""
        try:
            closer()
        except Exception as exc:
            shutdown_errors.append(f"{label}: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")

    try:
        pw = sync_playwright().start()
        browser = context = None
        try:
            # TTD_CHROMIUM_PATH lets you point at an existing Chromium instead of Playwright's download.
            browser = pw.chromium.launch(headless=not args.headed, executable_path=os.environ.get("TTD_CHROMIUM_PATH"))
            context = browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata")
            page = context.new_page()
            page.on("response", on_response)
            page.on("requestfailed", on_failed)

            print(f"Opening {url}")
            try:
                page.goto(url, wait_until="networkidle", timeout=60_000)
                time.sleep(args.wait)
                page.screenshot(path=str(run_dir / "page.png"), full_page=True)
                print(f"Title: {page.title()!r}")
            except PlaywrightError as exc:
                print(f"Page did not load: {exc.message.splitlines()[0]}", file=sys.stderr)
        finally:
            if context is not None:
                safe_close("context.close", context.close)
            if browser is not None:
                safe_close("browser.close", browser.close)
            safe_close("playwright.stop", pw.stop)
    except Exception as exc:
        shutdown_errors.append(f"run aborted: {type(exc).__name__}: {exc}")
    finally:
        write_outputs(run_dir, url, records, failures, shutdown_errors)

    return 0 if records else 1


def write_outputs(run_dir, url, records, failures, shutdown_errors):
    with (run_dir / "requests.jsonl").open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        for f in failures:
            fh.write(json.dumps({"failed": True, **f}, ensure_ascii=False) + "\n")
        for e in shutdown_errors:
            fh.write(json.dumps({"shutdown_error": e}, ensure_ascii=False) + "\n")

    api_records = [r for r in records if r["resource_type"] in API_TYPES]
    api = Counter(f'{r["method"]} {r["status"]} {r["host"]}{r["path"]}' for r in api_records)
    types = Counter(r["resource_type"] for r in records)
    lines = [
        f"URL: {url}",
        f"Responses recorded: {len(records)}  Failed requests: {len(failures)}  Shutdown errors: {len(shutdown_errors)}",
        "By type: " + ", ".join(f"{k}={v}" for k, v in types.most_common()),
        "",
        "XHR/fetch endpoints (method status host+path  x count):",
        *[f"  {k}  x{v}" for k, v in api.most_common()],
        "",
        f"Every XHR/fetch request ({len(api_records)}), in order:",
    ]
    for i, r in enumerate(api_records, 1):
        captured = f'yes ({r["body_file"]})' if r["json_body_captured"] else f'no{" - " + r["body_note"] if r.get("body_note") else ""}'
        lines += [
            f'  [{i}] {r["method"]} {r["status"]} {r["url"]}',
            f'      content-type: {r["content_type"] or "-"}',
            f'      query: {json.dumps(r["query"], ensure_ascii=False) if r["query"] else "-"}',
            f"      json body captured: {captured}",
        ]
    lines += [
        "",
        "Failed requests:",
        *[f'  {f["method"]} {f["url"]}  ({f["error"]})' for f in failures],
        "",
        "Shutdown errors (output was still written):",
        *[f"  {e}" for e in shutdown_errors],
    ]
    (run_dir / "summary.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nSaved to {run_dir}")

if __name__ == "__main__":
    sys.exit(main())
