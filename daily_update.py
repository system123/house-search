#!/usr/bin/env python3
"""Daily automation: status refresh + search attempt + preference scoring.

Reads config from search-config.json and updates listings.csv in place.
Emits a run summary as JSON to stdout for the orchestrating workflow.
"""
from __future__ import annotations

import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

BASE = Path(__file__).parent
CSV_PATH = BASE / "listings.csv"
CONFIG_PATH = BASE / "search-config.json"

TODAY = date.today().isoformat()

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT_S = 15
STATUS_WORKERS = 8
SEARCH_WORKERS = 6

REMOVED_PHRASES = (
    "no longer available",
    "listing has been removed",
    "property has been removed",
    "this listing is no longer",
    "property is no longer available",
    "page not found",
)
UNDER_OFFER_PHRASES = (
    "under offer",
    "offer pending",
    "sale pending",
)
SOLD_PHRASES = (
    "property sold",
    "status: sold",
    "this property has been sold",
    "sold stc",
)


# MARK: - CSV helpers

REQUIRED_COLUMNS = (
    "id", "url", "address", "area", "price", "bedrooms", "bathrooms",
    "garage", "flatlet", "garden", "agent_name", "agent_phone",
    "agent_email", "listing_status", "status", "notes", "date_added",
    "score", "last_checked", "score_reason",
)


def load_listings() -> tuple[list[dict[str, str]], list[str]]:
    with CSV_PATH.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = [dict(r) for r in reader]
        fieldnames = list(reader.fieldnames or [])
    for col in ("last_checked", "score_reason"):
        if col not in fieldnames:
            fieldnames.append(col)
        for row in rows:
            row.setdefault(col, "")
    return rows, fieldnames


def save_listings(rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open() as f:
        return json.load(f)


# MARK: - Status refresh

def classify_response(resp: requests.Response | None, err: str | None) -> tuple[str, str]:
    """Return (status_label, note). status_label in {active, under_offer, sold, removed, unknown, unreachable}."""
    if err is not None:
        return "unreachable", err
    assert resp is not None
    if resp.status_code == 404:
        return "removed", "404 Not Found"
    if resp.status_code in (410,):
        return "removed", f"HTTP {resp.status_code}"
    if resp.status_code >= 500:
        return "unreachable", f"HTTP {resp.status_code}"
    if resp.status_code in (403, 429):
        return "unreachable", f"HTTP {resp.status_code} (blocked)"
    if resp.status_code >= 400:
        return "unreachable", f"HTTP {resp.status_code}"

    text = resp.text.lower()
    if any(p in text for p in REMOVED_PHRASES):
        return "removed", "page body: removed/not available"
    if any(p in text for p in SOLD_PHRASES):
        return "sold", "page body: sold"
    if any(p in text for p in UNDER_OFFER_PHRASES):
        return "under_offer", "page body: under offer"
    return "active", ""


def fetch_status(url: str) -> tuple[str, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
    try:
        resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT_S, allow_redirects=True)
    except requests.exceptions.Timeout:
        return classify_response(None, "timeout")
    except requests.exceptions.ConnectionError as e:
        return classify_response(None, f"connection error: {type(e).__name__}")
    except requests.exceptions.RequestException as e:
        return classify_response(None, f"request error: {type(e).__name__}")
    return classify_response(resp, None)


def refresh_statuses(rows: list[dict[str, str]]) -> dict[str, Any]:
    terminal = {"sold", "removed"}
    to_check = [i for i, r in enumerate(rows) if r.get("listing_status", "").lower() not in terminal]
    checked = 0
    updated: list[dict[str, str]] = []
    unreachable_domains: set[str] = set()

    def work(idx: int) -> tuple[int, str, str]:
        label, note = fetch_status(rows[idx]["url"])
        return idx, label, note

    with ThreadPoolExecutor(max_workers=STATUS_WORKERS) as pool:
        futures = [pool.submit(work, i) for i in to_check]
        for fut in as_completed(futures):
            idx, label, note = fut.result()
            checked += 1
            row = rows[idx]
            prior = row.get("listing_status", "").lower()
            # Treat legacy "new" as equivalent to "active" for comparison purposes.
            prior_normalised = "active" if prior == "new" else prior

            if label == "unreachable":
                row["last_checked"] = TODAY
                existing = row.get("notes", "") or ""
                marker = f"Status check failed: {note}"
                if marker not in existing:
                    row["notes"] = (existing + " | " if existing else "") + marker
                try:
                    unreachable_domains.add(urlparse(row["url"]).netloc)
                except Exception:
                    pass
                continue

            row["last_checked"] = TODAY
            if label != prior_normalised:
                updated.append({
                    "id": row.get("id", ""),
                    "url": row["url"],
                    "from": prior or "unknown",
                    "to": label,
                })
                row["listing_status"] = label

    return {
        "checked": checked,
        "updated_count": len(updated),
        "updates": updated,
        "unreachable_domains": sorted(unreachable_domains),
    }


# MARK: - Search attempt

def attempt_source_search(source_url: str) -> dict[str, Any]:
    """Probe the homepage. This agent cannot render JavaScript-heavy SPA
    search results reliably, so each source is marked honestly as reachable
    but not harvestable (NO_RESULTS) or FAILED when the site refuses access.
    """
    domain = urlparse(source_url).netloc
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
    try:
        resp = requests.get(source_url, headers=headers, timeout=HTTP_TIMEOUT_S, allow_redirects=True)
    except requests.exceptions.Timeout:
        return {"source": domain, "outcome": "FAILED", "new_listings": 0,
                "pages_searched": 0, "error": "timeout"}
    except requests.exceptions.RequestException as e:
        return {"source": domain, "outcome": "FAILED", "new_listings": 0,
                "pages_searched": 0, "error": f"{type(e).__name__}"}

    if resp.status_code >= 400:
        return {"source": domain, "outcome": "FAILED", "new_listings": 0,
                "pages_searched": 0, "error": f"HTTP {resp.status_code}"}

    return {
        "source": domain,
        "outcome": "NO_RESULTS",
        "new_listings": 0,
        "pages_searched": 1,
        "note": "homepage reachable; JS-rendered search results not harvestable without a headless browser",
    }


def search_sources(sources: list[str]) -> dict[str, Any]:
    outcomes: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as pool:
        futures = {pool.submit(attempt_source_search, s): s for s in sources}
        for fut in as_completed(futures):
            outcomes.append(fut.result())
    outcomes.sort(key=lambda x: x["source"])
    return {
        "sources_attempted": len(sources),
        "source_outcomes": outcomes,
        "new_listings_added": 0,
        "duplicates_skipped": 0,
        "duplicates_url_updated": 0,
    }


# MARK: - Preference scoring

def _to_int(value: str) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def score_row(row: dict[str, str], config: dict[str, Any]) -> tuple[int, str]:
    price = _to_int(row.get("price", ""))
    beds = _to_int(row.get("bedrooms", ""))
    area = (row.get("area") or "").strip()
    regions = {r.lower() for r in config.get("regions", [])}
    budget_min = config["budget"]["min_zar"]
    budget_max = config["budget"]["max_zar"]
    beds_min = config["bedrooms_min"]

    reasons: list[str] = []
    score = 5

    if price is not None:
        if budget_min <= price <= budget_max:
            score += 1
            reasons.append("in budget")
        else:
            score -= 2
            reasons.append("out of budget")

    if beds is not None and beds >= beds_min:
        score += 1
        reasons.append(f"{beds}bed")

    if area and area.lower() in regions:
        score += 1
        reasons.append("region match")

    if (row.get("garden", "").lower() == "yes"):
        score += 1
        reasons.append("garden")
    if (row.get("flatlet", "").lower() == "yes"):
        score += 1
        reasons.append("flatlet")
    if (row.get("garage", "").lower() == "yes"):
        reasons.append("garage")
        score += 1

    status = (row.get("listing_status") or "").lower()
    if status in ("sold", "removed"):
        score = min(score, 2)
        reasons.append(f"status={status}")
    elif status == "under_offer":
        score = min(score, 6)
        reasons.append("under offer")

    score = max(1, min(10, score))
    reason_str = ", ".join(reasons)[:80]
    return score, reason_str


def apply_scoring(rows: list[dict[str, str]], config: dict[str, Any]) -> dict[str, Any]:
    interested = [r for r in rows if r.get("status", "").lower() == "interested"]
    rejected = [r for r in rows if r.get("status", "").lower() == "rejected"]
    basis_rows = len(interested) + len(rejected)

    if basis_rows < 5:
        basis = (
            "insufficient preference data; scoring from config-derived heuristics "
            "(budget fit, region match, beds, garden/flatlet/garage)"
        )
    else:
        basis = (
            f"preferences from {len(interested)} interested / {len(rejected)} rejected rows; "
            "heuristic scorer applied until richer signal is modelled"
        )

    scored = 0
    for row in rows:
        if row.get("status", "").lower() != "unseen":
            continue
        s, reason = score_row(row, config)
        row["score"] = str(s)
        row["score_reason"] = reason
        scored += 1

    return {"rows_scored": scored, "preference_basis": basis, "basis_rows": basis_rows}


# MARK: - Main

def main() -> int:
    config = load_config()
    rows, fieldnames = load_listings()

    status_result = refresh_statuses(rows)
    search_result = search_sources(list(config["sources"]))
    scoring_result = apply_scoring(rows, config)

    save_listings(rows, fieldnames)

    summary = {
        "run_date": TODAY,
        "status": status_result,
        "search": search_result,
        "scoring": scoring_result,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
