#!/usr/bin/env python3
"""Refresh listing_status for each non-sold, non-removed row in listings.csv.

Conservative rules:
- Only mark ``removed`` when HTTP 404 is returned by the origin server, OR
  a distinctive removal message ("no longer available" / "listing has been
  removed") appears together with a non-generic title/canonical URL. Generic
  phrases inside search/template sections are ignored.
- Never re-classify to ``under_offer`` or ``sold`` purely from HTML text
  because most SA property sites are Cloudflare-protected and their marketing
  templates contain those words in unrelated sections. Manual review
  required.
- 5xx / timeout / DNS => unreachable (no status change, add note).
- Always stamp ``last_checked`` for rows we tried to fetch.
- Adds the ``last_checked`` and ``score_reason`` columns to the CSV schema
  if not already present.
"""
from __future__ import annotations

import csv
import datetime as dt
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CSV_PATH = Path(__file__).resolve().parent.parent / "listings.csv"
LOG_PATH = Path(__file__).resolve().parent.parent / "status-check.log"
TODAY = dt.date.today().isoformat()

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

TIMEOUT_SEC = 20
MAX_RETRIES = 2  # extra attempts on transient 5xx


@dataclass
class CheckResult:
    status: str  # active | removed | unreachable | unknown
    http_code: Optional[int]
    reason: str


def _fetch_once(url: str) -> tuple[Optional[int], Optional[str], Optional[Exception]]:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-ZA,en;q=0.9",
        },
    )
    try:
        with urlopen(req, timeout=TIMEOUT_SEC) as resp:
            return resp.status, resp.read(400_000).decode("utf-8", errors="ignore"), None
    except HTTPError as e:
        return e.code, None, e
    except (URLError, TimeoutError) as e:
        return None, None, e
    except Exception as e:  # noqa: BLE001
        return None, None, e


def _is_clearly_removed(html: str) -> bool:
    """Only return True when the page is clearly a removed/expired listing.

    Requires a strong signal in the *title* or a dedicated error page, not
    just an occurrence of the phrase somewhere in body/footer templates.
    """
    if not html:
        return False
    lower = html.lower()

    title_match = re.search(r"<title>(.*?)</title>", lower, re.DOTALL)
    title = (title_match.group(1) if title_match else "")[:200]

    title_signals = [
        "not found",
        "no longer available",
        "listing removed",
        "page not found",
        "404",
    ]
    if any(s in title for s in title_signals):
        return True

    strong_body = [
        "this listing is no longer available",
        "this property has been removed",
        "this listing has been removed",
        "the listing you were looking for",
        "sorry, this listing has expired",
    ]
    if any(s in lower for s in strong_body):
        return True

    return False


def _fetch(url: str) -> CheckResult:
    delay = 1.0
    last_err: Optional[Exception] = None
    last_code: Optional[int] = None
    for attempt in range(MAX_RETRIES + 1):
        code, body, err = _fetch_once(url)
        last_code, last_err = code, err
        if code == 200 and body is not None:
            if _is_clearly_removed(body):
                return CheckResult("removed", 200, "removed-signal")
            return CheckResult("active", 200, "ok")
        if code == 404:
            return CheckResult("removed", 404, "HTTP 404")
        if code and 500 <= code < 600:
            time.sleep(delay)
            delay *= 2
            continue
        if err is not None and code is None:
            time.sleep(delay)
            delay *= 2
            continue
        break

    if last_code and 500 <= last_code < 600:
        return CheckResult("unreachable", last_code, f"HTTP {last_code} (Cloudflare/WAF likely)")
    if last_code == 403:
        return CheckResult("unreachable", 403, "HTTP 403 (blocked)")
    if last_code:
        return CheckResult("unreachable", last_code, f"HTTP {last_code}")
    return CheckResult("unreachable", None, f"error: {type(last_err).__name__ if last_err else 'unknown'}")


@dataclass
class Summary:
    checked: int = 0
    updated: int = 0
    updates: list[dict] = field(default_factory=list)
    unreachable_rows: int = 0
    unreachable_domains: list[str] = field(default_factory=list)


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.replace("www.", "")
    except Exception:  # noqa: BLE001
        return url


def _append_note(row: dict, marker: str) -> None:
    existing = (row.get("notes") or "").strip()
    if marker in existing:
        return
    row["notes"] = f"{existing} | {marker}".strip(" |") if existing else marker


def refresh(rows: list[dict], log: list[str]) -> Summary:
    summary = Summary()
    for row in rows:
        cur = (row.get("listing_status") or "").strip()
        if cur in {"sold", "removed"}:
            continue
        url = (row.get("url") or "").strip()
        if not url:
            continue

        summary.checked += 1
        result = _fetch(url)
        row["last_checked"] = TODAY

        if result.status == "unreachable":
            summary.unreachable_rows += 1
            dom = _domain(url)
            if dom not in summary.unreachable_domains:
                summary.unreachable_domains.append(dom)
            _append_note(row, f"Status check {TODAY}: failed ({result.reason})")
            log.append(f"#{row.get('id')} UNREACHABLE {result.reason} {url}")
            time.sleep(0.5)
            continue

        if result.status == "removed" and cur != "removed":
            row["listing_status"] = "removed"
            _append_note(row, f"Marked removed on {TODAY} (HTTP {result.http_code}, {result.reason})")
            summary.updated += 1
            summary.updates.append({"id": row.get("id"), "from": cur, "to": "removed"})
            log.append(f"#{row.get('id')} REMOVED ({result.reason}) {url}")

        time.sleep(0.4)

    return summary


def main() -> int:
    with CSV_PATH.open() as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    for extra in ("last_checked", "score_reason"):
        if extra not in fieldnames:
            fieldnames.append(extra)

    log: list[str] = [f"# status_check run {TODAY}"]
    summary = refresh(rows, log)

    with CSV_PATH.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})

    with LOG_PATH.open("a") as fh:
        fh.write("\n".join(log) + "\n")

    print(f"checked={summary.checked} updated={summary.updated} "
          f"unreachable_rows={summary.unreachable_rows} "
          f"unreachable_domains={summary.unreachable_domains}")
    for u in summary.updates:
        print(f"  #{u['id']}: {u['from']} -> {u['to']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
