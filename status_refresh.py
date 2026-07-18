#!/usr/bin/env python3
"""Status refresh: fetch each active listing URL and update listing_status.

- Marks 404 or "removed"/"no longer available" pages as removed.
- Detects sold/under_offer keywords in page body.
- On timeout / 5xx / connection error: leaves status untouched but stamps
  last_checked and appends a note.
"""
from __future__ import annotations

import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

import urllib3

urllib3.disable_warnings()

try:
    import requests
except ImportError:
    print("requests missing", file=sys.stderr)
    sys.exit(1)


BASE = Path(__file__).resolve().parent
CSV_PATH = BASE / "listings.csv"
TODAY = date.today().isoformat()

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-ZA,en;q=0.9",
}

REMOVED_PATTERNS = [
    r"\bno longer available\b",
    r"\bno longer for sale\b",
    r"\blisting (has been )?removed\b",
    r"\bproperty (has been )?removed\b",
    r"\bthis property has been (withdrawn|removed)\b",
    r"\bpage not found\b",
    r"\b404 - page not found\b",
]
SOLD_PATTERNS = [
    r"\bstatus:\s*sold\b",
    r"\bthis (property|listing) (has been |is )sold\b",
    r"\bproperty sold\b",
]
UNDER_OFFER_PATTERNS = [
    r"\bthis (property|listing) is under offer\b",
    r"\bstatus:\s*under[\s-]offer\b",
    r"\bsale pending\b",
    r"\boffer accepted\b",
]


@dataclass
class CheckResult:
    status: str  # active | under_offer | sold | removed | unknown | error
    detail: str = ""


def _search_any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def classify_body(body: str) -> CheckResult:
    if _search_any(REMOVED_PATTERNS, body):
        return CheckResult("removed", "matched removed pattern")
    if _search_any(SOLD_PATTERNS, body):
        return CheckResult("sold", "matched sold pattern")
    if _search_any(UNDER_OFFER_PATTERNS, body):
        return CheckResult("under_offer", "matched under_offer pattern")
    return CheckResult("unknown", "no status keywords")


def _path_tail(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    return parts[-1].lower() if parts else ""


def _redirected_away(requested: str, final: str) -> bool:
    """Return True if the final URL clearly dropped the specific listing."""
    if not final:
        return False
    req_tail = _path_tail(requested)
    fin_tail = _path_tail(final)
    if not req_tail or req_tail == fin_tail:
        return False
    parsed_final = urlparse(final)
    fin_path_lower = parsed_final.path.lower()
    if req_tail in fin_path_lower:
        return False
    return len(fin_path_lower) < len(urlparse(requested).path.lower())


def fetch(url: str, session: requests.Session, timeout: int = 20) -> CheckResult:
    try:
        r = session.get(
            url,
            headers=HEADERS,
            timeout=timeout,
            allow_redirects=True,
            verify=False,
        )
    except requests.exceptions.Timeout:
        return CheckResult("error", "timeout")
    except requests.exceptions.SSLError as exc:
        return CheckResult("error", f"ssl:{exc.__class__.__name__}")
    except requests.exceptions.ConnectionError as exc:
        return CheckResult("error", f"conn:{exc.__class__.__name__}")
    except requests.exceptions.RequestException as exc:
        return CheckResult("error", f"req:{exc.__class__.__name__}")

    if r.status_code == 404:
        return CheckResult("removed", "http 404")
    if 500 <= r.status_code < 600:
        return CheckResult("error", f"http {r.status_code}")
    if r.status_code in (401, 403):
        return CheckResult("error", f"http {r.status_code} blocked")
    if r.status_code != 200:
        return CheckResult("unknown", f"http {r.status_code}")

    if _redirected_away(url, r.url):
        return CheckResult("removed", f"redirected to {urlparse(r.url).path[:60]}")

    body = r.text or ""
    return classify_body(body)


def ensure_columns(fieldnames: list[str], rows: list[dict]) -> list[str]:
    changed = list(fieldnames)
    for col in ("last_checked", "score_reason"):
        if col not in changed:
            changed.append(col)
            for row in rows:
                row.setdefault(col, "")
    return changed


def append_note(row: dict, msg: str) -> None:
    existing = (row.get("notes") or "").strip()
    if msg in existing:
        return
    row["notes"] = f"{existing} | {msg}".strip(" |") if existing else msg


def process_row(row: dict, session: requests.Session) -> tuple[str, str]:
    """Return (change_description, outcome_tag).

    change_description is "" when nothing changed.
    outcome_tag is one of: updated / unchanged / unreachable / skipped.
    """
    current = (row.get("listing_status") or "").strip().lower()
    if current in {"sold", "removed"}:
        return "", "skipped"

    url = (row.get("url") or "").strip()
    if not url or not url.startswith("http"):
        return "", "skipped"

    result = fetch(url, session)
    row["last_checked"] = TODAY

    if result.status == "error":
        append_note(row, f"Status check failed: {result.detail}")
        return "", "unreachable"

    new_status = result.status
    if new_status == "unknown":
        return "", "unchanged"

    normalised_current = "active" if current in {"new", "active", ""} else current
    # Conservative: never demote under_offer back to active on keyword absence.
    if normalised_current == "under_offer" and new_status == "active":
        return "", "unchanged"
    if new_status != normalised_current:
        row["listing_status"] = new_status
        return f"{current or 'active'}->{new_status}", "updated"
    return "", "unchanged"


def main() -> None:
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    fieldnames = ensure_columns(fieldnames, rows)

    session = requests.Session()
    checked = 0
    updated = 0
    changes: list[str] = []
    unreachable_domains: dict[str, int] = {}

    for row in rows:
        current = (row.get("listing_status") or "").strip().lower()
        if current in {"sold", "removed"}:
            continue
        url = (row.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        checked += 1
        change, outcome = process_row(row, session)
        if outcome == "updated":
            updated += 1
            changes.append(f"id {row.get('id')}: {change}")
        elif outcome == "unreachable":
            host = urlparse(url).netloc or "unknown"
            unreachable_domains[host] = unreachable_domains.get(host, 0) + 1
        time.sleep(0.4)

    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"CHECKED={checked}")
    print(f"UPDATED={updated}")
    for change in changes:
        print(f"CHANGE {change}")
    for host, count in unreachable_domains.items():
        print(f"UNREACHABLE {host} {count}")


if __name__ == "__main__":
    main()
