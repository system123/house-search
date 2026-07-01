"""Shared helpers for the property-search automation scripts."""
from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
import urllib3

urllib3.disable_warnings()

BASE_DIR: Path = Path(__file__).resolve().parent.parent
CSV_PATH: Path = BASE_DIR / "listings.csv"
CONFIG_PATH: Path = BASE_DIR / "search-config.json"
RUN_LOG_PATH: Path = BASE_DIR / "run-log.jsonl"
TODAY: str = dt.date.today().isoformat()

CANONICAL_COLUMNS: list[str] = [
    "id", "url", "address", "area", "price", "bedrooms", "bathrooms",
    "garage", "flatlet", "garden", "agent_name", "agent_phone", "agent_email",
    "listing_status", "status", "notes", "date_added", "score",
    "last_checked", "score_reason",
]

USER_AGENT: str = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
HTTP_HEADERS: dict[str, str] = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


@dataclass
class FetchResult:
    """Outcome of an HTTP fetch."""
    ok: bool
    status: int | None
    url_final: str
    text: str
    error: str


def domain_of(url: str) -> str:
    """Return lowercased hostname of *url* with any leading www. stripped."""
    host = urlparse(url).hostname or ""
    return host.lower().removeprefix("www.")


def http_get(url: str, timeout: int = 20) -> FetchResult:
    """Fetch *url* and return a FetchResult; never raises."""
    try:
        resp = requests.get(
            url, headers=HTTP_HEADERS, timeout=timeout,
            allow_redirects=True, verify=False,
        )
    except requests.exceptions.Timeout:
        return FetchResult(False, None, url, "", "timeout")
    except requests.exceptions.ConnectionError as exc:
        return FetchResult(False, None, url, "", f"connection: {exc.__class__.__name__}")
    except requests.RequestException as exc:
        return FetchResult(False, None, url, "", f"request: {exc.__class__.__name__}")
    body = resp.text or ""
    if _looks_like_cloudflare(body):
        return FetchResult(False, resp.status_code, resp.url, body, "cloudflare-challenge")
    ok = 200 <= resp.status_code < 400
    return FetchResult(ok, resp.status_code, resp.url, body, "" if ok else f"http {resp.status_code}")


def _looks_like_cloudflare(body: str) -> bool:
    """Heuristic: Cloudflare 'Just a moment...' or turnstile pages."""
    if not body:
        return False
    return bool(re.search(r"(Just a moment\.\.\.|cf-browser-verification|cf_chl_)", body[:2000]))


def read_listings() -> tuple[list[str], list[dict[str, str]]]:
    """Load listings.csv. Ensures canonical columns exist on every row."""
    with CSV_PATH.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = [dict(r) for r in reader]
    headers = list(CANONICAL_COLUMNS)
    for row in rows:
        for col in CANONICAL_COLUMNS:
            row.setdefault(col, "")
    return headers, rows


def write_listings(rows: Iterable[dict[str, str]]) -> None:
    """Write rows back to listings.csv using canonical column order."""
    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CANONICAL_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: (row.get(c, "") or "") for c in CANONICAL_COLUMNS})


def append_note(existing: str, addition: str) -> str:
    """Append *addition* to *existing* notes, avoiding exact-duplicate lines.

    Uses ' | ' as separator to keep the CSV free of unquoted commas.
    """
    parts = [p.strip() for p in (existing or "").split("|") if p.strip()]
    if addition and addition not in parts:
        parts.append(addition)
    return " | ".join(parts)
