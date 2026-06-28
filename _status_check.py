#!/usr/bin/env python3
"""Conservative status refresh for listings.csv.

Approach:
- HTTP 404/410 -> listing_status = removed (definitive).
- Other 4xx/5xx/timeout/conn errors -> note "Status check failed: ..." and
  leave listing_status unchanged.
- HTTP 200/3xx -> conservatively keep current status, unless very specific
  banner markers indicate "sold" or "under offer" near the listing header.
- Adds columns last_checked and score_reason if missing.
"""
from __future__ import annotations

import csv
import datetime as dt
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import urllib3
import requests

urllib3.disable_warnings()

BASE = Path(__file__).parent
CSV_PATH = BASE / "listings.csv"
TODAY = dt.date.today().isoformat()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Very specific patterns that indicate the *current listing* has changed status.
# Generic words like "sold" or "under offer" alone are not used because they
# appear in site navigation ("Sold By Us"), related-listing cards, and footers.
SOLD_BANNER = re.compile(
    r'(class="[^"]*\b(?:sold-banner|status--sold|listing-sold|property-sold)\b'
    r'|data-status="sold"'
    r'|property has been sold'
    r'|this property is sold'
    r'|<title>[^<]*\bsold\b[^<]*</title>)',
    re.IGNORECASE,
)
UNDER_OFFER_BANNER = re.compile(
    r'(class="[^"]*\b(?:under-offer|status--under-offer|listing-under-offer)\b'
    r'|data-status="under[-_ ]offer"'
    r'|this property is under offer'
    r'|<title>[^<]*\bunder offer\b[^<]*</title>)',
    re.IGNORECASE,
)
REMOVED_BANNER = re.compile(
    r"(no longer available|listing has been removed|listing not found"
    r"|property not found|page not found|listing is unavailable)",
    re.IGNORECASE,
)


@dataclass
class CheckResult:
    status: str  # active | under_offer | sold | removed | unreachable | unknown
    detail: str = ""
    http_code: Optional[int] = None


def classify_page(html: str) -> Optional[str]:
    if REMOVED_BANNER.search(html):
        return "removed"
    if SOLD_BANNER.search(html):
        return "sold"
    if UNDER_OFFER_BANNER.search(html):
        return "under_offer"
    return None


def _final_path_drops_listing_id(orig_url: str, final_url: str) -> bool:
    """True if the final URL no longer contains the original listing's tail
    segment (numeric id or alphanumeric listing slug like T1234567 / kw1234567)."""
    m = re.search(r"/([A-Za-z]{0,3}\d{4,})/?$", orig_url.rstrip("/"))
    if not m:
        return False
    tail = m.group(1)
    return tail.lower() not in final_url.lower()


def check_url(url: str) -> CheckResult:
    if not url or not url.startswith("http"):
        return CheckResult("unreachable", "invalid URL")
    try:
        resp = requests.get(
            url, headers=HEADERS, timeout=20, allow_redirects=True, verify=False
        )
    except requests.exceptions.Timeout:
        return CheckResult("unreachable", "timeout")
    except requests.exceptions.ConnectionError as exc:
        return CheckResult("unreachable", f"conn_error:{type(exc).__name__}")
    except Exception as exc:
        return CheckResult("unreachable", f"error:{type(exc).__name__}")

    code = resp.status_code
    if code in (404, 410):
        return CheckResult("removed", f"http_{code}", code)
    if code >= 500:
        return CheckResult("unreachable", f"http_{code}", code)
    if code in (401, 403, 429):
        return CheckResult("unreachable", f"blocked_http_{code}", code)
    if code >= 400:
        return CheckResult("unreachable", f"http_{code}", code)

    # If we were redirected away from the original listing-id URL to a category
    # or search page, treat as removed (the listing detail no longer exists).
    if _final_path_drops_listing_id(url, resp.url):
        return CheckResult("removed", f"redirected_to:{resp.url[:80]}", code)

    detected = classify_page(resp.text)
    if detected:
        return CheckResult(detected, "page_banner", code)
    return CheckResult("unknown", "ok", code)


def domain_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)/?", url)
    return m.group(1) if m else url


def main() -> int:
    with CSV_PATH.open() as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    if "last_checked" not in fieldnames:
        fieldnames.append("last_checked")
    if "score_reason" not in fieldnames:
        fieldnames.append("score_reason")
    for r in rows:
        r.setdefault("last_checked", "")
        r.setdefault("score_reason", "")

    targets = [r for r in rows if r["listing_status"] not in ("sold", "removed")]
    print(f"Checking {len(targets)} rows...", file=sys.stderr)

    results: dict[str, CheckResult] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(check_url, r["url"]): r["id"] for r in targets}
        for fut in as_completed(futures):
            rid = futures[fut]
            results[rid] = fut.result()

    summary = {
        "checked": len(targets),
        "updated": 0,
        "updates": [],
        "unreachable_domains": [],
    }
    unreachable = set()

    for r in targets:
        rid = r["id"]
        old = r["listing_status"]
        res = results[rid]
        r["last_checked"] = TODAY

        if res.status == "unreachable":
            unreachable.add(domain_of(r["url"]))
            note = f"Status check failed: {res.detail}"
            existing = r.get("notes", "") or ""
            if note not in existing:
                r["notes"] = f"{existing} | {note}".strip(" |") if existing else note
            continue

        if res.status == "unknown":
            # Page reachable but no definitive banner — keep recorded status.
            continue

        if res.status != old:
            summary["updated"] += 1
            summary["updates"].append(
                {"id": rid, "url": r["url"], "from": old, "to": res.status, "detail": res.detail}
            )
            r["listing_status"] = res.status

    summary["unreachable_domains"] = sorted(unreachable)

    with CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    import json
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
