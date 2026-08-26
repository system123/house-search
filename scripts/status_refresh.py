#!/usr/bin/env python3
"""Refresh listing_status for each row in listings.csv whose current
listing_status is not 'sold' or 'removed'. Writes results back to CSV
and returns a summary dict serialized to stdout as JSON.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

BASE = Path(__file__).resolve().parent.parent
CSV_PATH = BASE / "listings.csv"
TODAY = date.today().isoformat()

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-ZA,en;q=0.9"}
TIMEOUT_S = 15

REMOVED_MARKERS = (
    "no longer available",
    "listing has been removed",
    "property is no longer",
    "this property is not available",
    "page you requested could not be found",
    "sorry, we can't find that page",
    "sorry, this property is no longer",
    "listing not found",
    "property not found",
)
SOLD_MARKERS = (
    "status: sold",
    ">sold<",
    "this property has been sold",
    "sold subject to",
)
UNDER_OFFER_MARKERS = (
    "under offer",
    "under-offer",
    "sale pending",
    "sale-pending",
)


@dataclass
class CheckResult:
    status: str  # active | under_offer | sold | removed | unknown
    reason: str = ""


@dataclass
class RunSummary:
    checked: int = 0
    updated: int = 0
    updates: list[dict] = field(default_factory=list)
    unreachable_domains: list[str] = field(default_factory=list)


def _classify_body(body: str) -> CheckResult:
    text = body.lower()
    if any(m in text for m in REMOVED_MARKERS):
        return CheckResult("removed", "page indicates listing removed")
    if any(m in text for m in SOLD_MARKERS):
        return CheckResult("sold", "page indicates sold")
    if any(m in text for m in UNDER_OFFER_MARKERS):
        return CheckResult("under_offer", "page indicates under offer")
    return CheckResult("active", "page loaded normally")


def _final_url_indicates_delisted(original: str, final: str) -> bool:
    """Detect when the site redirected away from a specific listing page
    (e.g. to a suburb / category / archive page). We compare the final path
    depth vs the original — a shorter path indicates the listing was
    delisted and replaced by a broader page.
    """
    if final == original:
        return False
    orig_path = urlparse(original).path.rstrip("/")
    final_path = urlparse(final).path.rstrip("/")
    if not orig_path or not final_path:
        return False
    orig_parts = [p for p in orig_path.split("/") if p]
    final_parts = [p for p in final_path.split("/") if p]
    if not orig_parts:
        return False
    last_segment = orig_parts[-1]
    if last_segment and last_segment not in final_parts:
        return True
    if len(final_parts) < len(orig_parts) - 1:
        return True
    if "archiveid=" in urlparse(final).query.lower():
        return True
    return False


def _final_url_indicates_sold(final: str) -> bool:
    """Some sites (e.g. Pam Golding) change the URL path from 'for-sale'
    to 'sold' when a property is marked sold.
    """
    path = urlparse(final).path.lower()
    if "/house-sold-" in path or "/property-sold-" in path:
        return True
    if re.search(r"/sold[-/]", path):
        return True
    return False


def check_url(session: requests.Session, url: str) -> CheckResult:
    try:
        resp = session.get(url, headers=HEADERS, timeout=TIMEOUT_S, allow_redirects=True)
    except requests.exceptions.RequestException as exc:
        return CheckResult("unknown", f"unreachable: {type(exc).__name__}")

    if resp.status_code == 404:
        return CheckResult("removed", "HTTP 404")
    if resp.status_code >= 500:
        return CheckResult("unknown", f"HTTP {resp.status_code}")
    if resp.status_code in (401, 403, 429):
        return CheckResult("unknown", f"HTTP {resp.status_code} (blocked)")
    if resp.status_code >= 400:
        return CheckResult("unknown", f"HTTP {resp.status_code}")

    final_url = str(resp.url)
    if _final_url_indicates_sold(final_url):
        return CheckResult("sold", "URL rewritten to sold path")
    if _final_url_indicates_delisted(url, final_url):
        return CheckResult("removed", "redirected to category/archive page")

    return _classify_body(resp.text)


def _append_note(existing: str, new_note: str) -> str:
    if not existing:
        return new_note
    if new_note in existing:
        return existing
    sep = " | " if not existing.endswith(".") else " "
    return f"{existing}{sep}{new_note}"


def process_row(session: requests.Session, row: dict, summary: RunSummary) -> None:
    current = (row.get("listing_status") or "").strip().lower()
    if current in ("sold", "removed"):
        return
    summary.checked += 1
    url = (row.get("url") or "").strip()
    if not url:
        return
    result = check_url(session, url)
    if result.status == "unknown":
        domain = urlparse(url).netloc.lower()
        if domain and domain not in summary.unreachable_domains:
            summary.unreachable_domains.append(domain)
        row["notes"] = _append_note(
            row.get("notes", ""), f"Status check failed: {result.reason} ({TODAY})"
        )
        row["last_checked"] = TODAY
        return

    new_status = result.status
    old = current or "unknown"
    if new_status != current:
        row["listing_status"] = new_status
        summary.updated += 1
        summary.updates.append({"id": row.get("id"), "from": old, "to": new_status, "url": url})
    row["last_checked"] = TODAY


def load_rows(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    return fieldnames, rows


def ensure_columns(fieldnames: list[str], extra: list[str]) -> list[str]:
    for col in extra:
        if col not in fieldnames:
            fieldnames.append(col)
    return fieldnames


def save_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            for col in fieldnames:
                row.setdefault(col, "")
            writer.writerow(row)


def main() -> int:
    fieldnames, rows = load_rows(CSV_PATH)
    fieldnames = ensure_columns(fieldnames, ["last_checked", "score_reason"])
    summary = RunSummary()

    with requests.Session() as session:
        for row in rows:
            process_row(session, row, summary)

    save_rows(CSV_PATH, fieldnames, rows)
    print(json.dumps({
        "checked": summary.checked,
        "updated": summary.updated,
        "updates": summary.updates,
        "unreachable_domains": summary.unreachable_domains,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
