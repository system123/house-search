#!/usr/bin/env python3
"""Step 2 of property-search automation: refresh listing_status for active rows.

Reads listings.csv, fetches each URL whose listing_status is not sold/removed,
classifies the response, and writes the CSV back. Also emits a JSON summary on
stdout for the orchestrator to pick up.
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import urllib3
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "listings.csv"
TODAY = date.today().isoformat()

REQUIRED_COLS = [
    "id", "url", "address", "area", "price", "bedrooms", "bathrooms",
    "garage", "flatlet", "garden", "agent_name", "agent_phone", "agent_email",
    "listing_status", "status", "notes", "date_added", "score",
    "last_checked", "score_reason",
]

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-ZA,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

http = urllib3.PoolManager(
    timeout=urllib3.Timeout(connect=8.0, read=15.0),
    retries=Retry(total=1, backoff_factor=0.5, status_forcelist=[]),
    headers=HEADERS,
)


@dataclass
class FetchResult:
    status_code: Optional[int]
    body: str
    title: str
    error: Optional[str]
    final_url: Optional[str]


def fetch(url: str) -> FetchResult:
    try:
        resp = http.request("GET", url, redirect=True, preload_content=True)
    except Exception as exc:  # noqa: BLE001
        return FetchResult(None, "", "", f"{type(exc).__name__}: {exc}", None)
    body_bytes = resp.data or b""
    try:
        body = body_bytes.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        body = ""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    final_url = resp.geturl() if hasattr(resp, "geturl") else url
    return FetchResult(resp.status, body, title, None, final_url)


def classify(url: str, r: FetchResult) -> tuple[str, str]:
    """Return (new_status_or_'keep', detail). 'unreachable' means leave row alone."""
    if r.error:
        return "unreachable", r.error
    code = r.status_code or 0
    if code == 404 or code == 410:
        return "removed", f"HTTP {code}"
    if code >= 500 or code in (403, 429):
        return "unreachable", f"HTTP {code}"
    if code != 200:
        return "unreachable", f"HTTP {code}"

    body = r.body
    title = r.title
    low_title = title.lower()
    if "just a moment" in body.lower() or "attention required" in low_title:
        return "unreachable", "Cloudflare challenge"

    host = urlparse(url).netloc.lower()

    if "privateproperty.co.za" in host:
        anchor = body.lower().find("listing-banners--details")
        scope = body.lower()[anchor : anchor + 3000] if anchor >= 0 else ""
        if scope:
            if "listing-banner--sold" in scope:
                return "sold", "banner sold"
            if "listing-banner--offer-pending" in scope:
                return "under_offer", "banner offer-pending"
            return "active", "banner default"
        if "no longer available" in body.lower():
            return "removed", "no longer available text"
        if re.search(r"property reference[: ]+T\d+", body, re.I):
            return "active", "reference id present"
        return "removed", "no listing banner / redirected"

    if "pamgolding.co.za" in host:
        if re.search(r"\d+\s+Properties\s+(?:and\s+Homes\s+)?For\s+Sale", title, re.I):
            return "removed", "redirected to search"
        if re.search(r"\b(House\s+)?Sold\b", title, re.I):
            return "sold", "title Sold"
        if re.search(r"\bUnder\s+Offer\b", title, re.I):
            return "under_offer", "title Under Offer"
        return "active", "detail page reachable"

    if "/results/residential/" in url:
        if re.search(r"^\s*\d+\s+(properties|houses|homes)", title, re.I):
            return "removed", "redirected to search"
        if re.search(r"\bSold\b", title, re.I):
            return "sold", "title Sold"
        if re.search(r"\bUnder\s+Offer\b", title, re.I):
            return "under_offer", "title Under Offer"
        return "active", "detail page reachable"

    if "property24.com" in host:
        if "this property has been sold" in body.lower():
            return "sold", "page text"
        if "no longer available" in body.lower():
            return "removed", "page text"
        if re.search(r"\bunder offer\b", body, re.I):
            return "under_offer", "page text"
        return "active", "page reachable"

    return "unreachable", "no site-specific classifier"


def main() -> int:
    rows: list[dict] = []
    with CSV_PATH.open() as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            for col in REQUIRED_COLS:
                row.setdefault(col, "")
            rows.append(row)

    skip = {"sold", "removed"}
    targets = [r for r in rows if (r.get("listing_status") or "").lower() not in skip]

    checked = 0
    updates: list[dict] = []
    unreachable_domains: dict[str, list[int]] = {}

    for row in targets:
        url = (row.get("url") or "").strip()
        if not url:
            continue
        checked += 1
        host = urlparse(url).netloc.lower()
        result = fetch(url)
        verdict, detail = classify(url, result)
        prev = (row.get("listing_status") or "").lower()
        if verdict == "unreachable":
            unreachable_domains.setdefault(host, []).append(int(row["id"]))
            row["last_checked"] = TODAY
            existing_notes = row.get("notes") or ""
            stamp = f"Status check failed ({TODAY}): {detail}"
            if stamp not in existing_notes:
                sep = " | " if existing_notes else ""
                row["notes"] = f"{existing_notes}{sep}{stamp}"
        else:
            row["last_checked"] = TODAY
            if verdict != prev:
                row["listing_status"] = verdict
                updates.append({
                    "id": row["id"], "url": url, "from": prev, "to": verdict,
                    "reason": detail,
                })
        time.sleep(0.4)

    fieldnames = REQUIRED_COLS
    with CSV_PATH.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})

    summary = {
        "checked": checked,
        "updated": len(updates),
        "updates": updates,
        "unreachable_domains": sorted(unreachable_domains.keys()),
        "unreachable_by_domain": {k: len(v) for k, v in unreachable_domains.items()},
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
