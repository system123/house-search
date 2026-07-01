#!/usr/bin/env python3
"""Step 2 — status refresh for all listings that are not sold/removed.

Fetches each URL and applies site-specific classifiers to detect
under_offer / sold / removed transitions. On unreachable errors, records
a same-day note but never changes listing_status.

Emits a JSON summary on stdout for the run-log to consume.
"""
from __future__ import annotations

import concurrent.futures as futures
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Callable

from common import (
    TODAY, append_note, domain_of, http_get, read_listings, write_listings,
    FetchResult,
)


@dataclass
class CheckOutcome:
    """Result of classifying a single listing URL."""
    listing_status: str | None  # None => leave unchanged
    unreachable_reason: str = ""
    detail: str = ""


REMOVED_TITLE_INDEX = re.compile(
    r"<title[^>]*>\s*\d+\s+(?:Properties|Houses|Homes)\b",
    re.IGNORECASE,
)
REMOVED_TEXT = re.compile(
    r"(no longer available|listing has been removed|property has been removed"
    r"|listing not found|property not found)",
    re.IGNORECASE,
)


def _title_of(body: str) -> str:
    match = re.search(r"<title[^>]*>([^<]*)</title>", body, re.IGNORECASE)
    return (match.group(1) or "").strip() if match else ""


def _classify_privateproperty(body: str, final_url: str) -> CheckOutcome:
    """Classify a privateproperty.co.za listing page."""
    if REMOVED_TITLE_INDEX.search(body) or "/for-sale/western-cape/cape-town/southern-suburbs/" in final_url and "/T" not in final_url.split("/")[-1]:
        if not re.search(r"listing-banners--details", body):
            return CheckOutcome("removed", detail="index page redirect")
    window_match = re.search(r"listing-banners--details[\s\S]{0,3000}", body)
    window = window_match.group(0) if window_match else body[:5000]
    if re.search(r"listing-banner--sold", window):
        return CheckOutcome("sold", detail="sold banner")
    if re.search(r"listing-banner--offer-pending", window):
        return CheckOutcome("under_offer", detail="offer banner")
    if REMOVED_TEXT.search(body):
        return CheckOutcome("removed", detail="removed text")
    return CheckOutcome(None)


def _classify_pamgolding(body: str) -> CheckOutcome:
    """Classify a pamgolding.co.za listing page."""
    title = _title_of(body)
    if re.match(r"\d+\s+Properties\b", title, re.IGNORECASE):
        return CheckOutcome("removed", detail="title index redirect")
    if re.search(r"\bSold\b", title):
        return CheckOutcome("sold", detail="title sold")
    if re.search(r"\bUnder Offer\b", title, re.IGNORECASE):
        return CheckOutcome("under_offer", detail="title under offer")
    if REMOVED_TEXT.search(body):
        return CheckOutcome("removed", detail="removed text")
    return CheckOutcome(None)


def _classify_seeff(body: str) -> CheckOutcome:
    """Classify a www.seeff.com listing page."""
    title = _title_of(body)
    if re.match(r"\d+\s+(properties|houses|homes)\b", title, re.IGNORECASE):
        return CheckOutcome("removed", detail="title index redirect")
    if re.search(r"\bSold\b", title):
        return CheckOutcome("sold", detail="title sold")
    if re.search(r"\bUnder Offer\b", title, re.IGNORECASE):
        return CheckOutcome("under_offer", detail="title under offer")
    return CheckOutcome(None)


def _classify_property24(body: str, final_url: str) -> CheckOutcome:
    """Classify a property24.com listing page."""
    title = _title_of(body)
    # If final URL dropped listing id we've landed on an index page (removed).
    tail_match = re.search(r"/(\d{6,})/?$", final_url)
    if not tail_match:
        if re.search(r"For Sale in [^<]+</title>", body[:2000]):
            return CheckOutcome("removed", detail="redirected to area index")
    if re.search(r"\bSold\b", title):
        return CheckOutcome("sold", detail="title sold")
    if re.search(r"\bUnder Offer\b", title, re.IGNORECASE):
        return CheckOutcome("under_offer", detail="title under offer")
    if re.search(r'class="[^"]*p24_status[^"]*"[^>]*>\s*Sold', body, re.IGNORECASE):
        return CheckOutcome("sold", detail="p24 status sold")
    if re.search(r'class="[^"]*p24_status[^"]*"[^>]*>\s*Under Offer', body, re.IGNORECASE):
        return CheckOutcome("under_offer", detail="p24 status under offer")
    return CheckOutcome(None)


CLASSIFIERS: dict[str, Callable[[str, str], CheckOutcome]] = {
    "privateproperty.co.za": lambda body, url: _classify_privateproperty(body, url),
    "pamgolding.co.za": lambda body, _url: _classify_pamgolding(body),
    "seeff.com": lambda body, _url: _classify_seeff(body),
    "southernsuburbs.seeff.com": lambda body, _url: _classify_seeff(body),
    "property24.com": lambda body, url: _classify_property24(body, url),
}


def classify_response(url: str, fetch: FetchResult) -> CheckOutcome:
    """Turn a FetchResult into a CheckOutcome using site-specific logic."""
    if fetch.status in (404, 410):
        return CheckOutcome("removed", detail=f"http {fetch.status}")
    if not fetch.ok:
        return CheckOutcome(None, unreachable_reason=fetch.error or f"http {fetch.status}")
    domain = domain_of(fetch.url_final or url)
    classifier = CLASSIFIERS.get(domain)
    if classifier is None:
        return CheckOutcome(None)  # unknown site — leave listing unchanged
    return classifier(fetch.text, fetch.url_final)


@dataclass
class RowUpdate:
    """Aggregated update to apply back to a listings row."""
    idx: int
    row: dict[str, str]
    changed_from: str = ""
    changed_to: str = ""
    unreachable: str = ""
    note_added: str = ""
    updated: bool = field(default=False)


def _process_row(idx: int, row: dict[str, str]) -> RowUpdate:
    """Fetch and classify a single row, returning the intended update."""
    upd = RowUpdate(idx=idx, row=row)
    url = (row.get("url") or "").strip()
    if not url.startswith("http"):
        upd.unreachable = "invalid URL"
        return upd
    fetch = http_get(url)
    outcome = classify_response(url, fetch)
    if outcome.listing_status:
        current = row.get("listing_status", "") or ""
        if outcome.listing_status != current:
            upd.changed_from = current
            upd.changed_to = outcome.listing_status
            upd.updated = True
        return upd
    if outcome.unreachable_reason:
        upd.unreachable = outcome.unreachable_reason
    return upd


def refresh_all() -> dict[str, object]:
    """Run status refresh across all non-sold/non-removed rows."""
    headers, rows = read_listings()
    del headers  # canonical order enforced in write_listings
    todo = [(i, r) for i, r in enumerate(rows)
            if (r.get("listing_status") or "") not in {"sold", "removed"}]
    updates: list[RowUpdate] = []
    with futures.ThreadPoolExecutor(max_workers=6) as pool:
        for res in pool.map(lambda pair: _process_row(*pair), todo):
            updates.append(res)
    _apply_updates(rows, updates)
    write_listings(rows)
    return _summarise(updates)


def _apply_updates(rows: list[dict[str, str]], updates: list[RowUpdate]) -> None:
    """Mutate *rows* in place based on the outcomes in *updates*."""
    for upd in updates:
        row = rows[upd.idx]
        row["last_checked"] = TODAY
        if upd.updated:
            row["listing_status"] = upd.changed_to
        if upd.unreachable:
            note = f"Status check failed ({TODAY}): {upd.unreachable}"
            row["notes"] = append_note(row.get("notes", ""), note)
            upd.note_added = note


def _summarise(updates: list[RowUpdate]) -> dict[str, object]:
    """Compact summary suitable for the run-log."""
    transitions: dict[str, int] = {}
    unreachable_domains: dict[str, int] = {}
    for upd in updates:
        if upd.updated:
            key = f"{upd.changed_from}->{upd.changed_to}"
            transitions[key] = transitions.get(key, 0) + 1
        if upd.unreachable:
            dom = domain_of(upd.row.get("url", ""))
            unreachable_domains[dom] = unreachable_domains.get(dom, 0) + 1
    return {
        "checked": len(updates),
        "updated": sum(1 for u in updates if u.updated),
        "transitions": transitions,
        "unreachable_domains": unreachable_domains,
    }


def main() -> int:
    summary = refresh_all()
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
