#!/usr/bin/env python3
"""Search each configured source for new listings that match the config
criteria (budget, bedrooms, regions). Records a per-source outcome and
appends genuinely new listings to listings.csv.

The implementation intentionally errs on the side of being conservative:
- Uses site-specific URL patterns for search when known.
- Extracts candidate listing URLs from the HTML using per-site regexes.
- Fetches each candidate to extract minimal metadata; when parsing fails
  the row is still recorded (with unknown fields) so a human can review.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

import requests

BASE = Path(__file__).resolve().parent.parent
CSV_PATH = BASE / "listings.csv"
CONFIG_PATH = BASE / "search-config.json"
TODAY = date.today().isoformat()
OUT_JSON = Path("/tmp/search_out.json")

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-ZA,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
FETCH_TIMEOUT_S = 15
PER_SOURCE_MAX_S = 90
PAGE_SLEEP_S = 1.0

# Small subset of regions with historic yield — we prioritise these to
# stay within the per-source time budget. Falls back to full config
# regions when the site supports a broad search.
PRIORITY_REGIONS = [
    "Pinelands", "Rondebosch", "Claremont", "Plumstead", "Bergvliet",
    "Meadowridge", "Kenilworth Upper", "Lakeside", "Rosebank", "Tokai",
]


@dataclass
class SourceOutcome:
    source: str
    outcome: str = "NO_RESULTS"  # SUCCESS | PARTIAL | NO_RESULTS | FAILED
    new_listings: int = 0
    pages_searched: int = 0
    error: str = ""


@dataclass
class SearchState:
    session: requests.Session
    existing_urls: set[str]
    existing_addresses: set[tuple[str, str]]
    config: dict
    outcomes: list[SourceOutcome] = field(default_factory=list)
    new_rows: list[dict] = field(default_factory=list)
    duplicates_skipped: int = 0
    duplicates_url_updated: int = 0


def _slug(region: str) -> str:
    return region.lower().replace(" ", "-")


def _load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def _load_existing(csv_path: Path) -> tuple[list[str], list[dict]]:
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def _index_existing(rows: list[dict]) -> tuple[set[str], set[tuple[str, str]]]:
    urls = {r["url"].strip().rstrip("/") for r in rows if r.get("url")}
    pairs = {
        (r.get("area", "").strip().lower(), r.get("address", "").strip().lower())
        for r in rows
        if r.get("address") and r.get("address") != "unknown"
    }
    return urls, pairs


def _fetch(state: SearchState, url: str) -> Optional[requests.Response]:
    try:
        resp = state.session.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT_S)
        return resp
    except requests.exceptions.RequestException:
        return None


def _within_budget(state: SearchState, price_zar: Optional[int], beds: Optional[int]) -> bool:
    cfg = state.config
    if price_zar is not None and price_zar > 0:
        if price_zar < cfg["budget"]["min_zar"] or price_zar > cfg["budget"]["max_zar"]:
            return False
    if beds is not None and beds < cfg["bedrooms_min"]:
        return False
    return True


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()


REGION_SLUG_INDEX: dict[str, str] = {}


def _build_region_index(regions: list[str]) -> None:
    REGION_SLUG_INDEX.clear()
    for region in regions:
        REGION_SLUG_INDEX[_slug(region)] = region


def _infer_area(url: str, fallback: str) -> str:
    parts = re.split(r"[/\-]", urlparse(url).path.lower())
    for i, part in enumerate(parts):
        if part in REGION_SLUG_INDEX:
            return REGION_SLUG_INDEX[part]
    # Two-word slugs may appear joined with a dash — check consecutive pairs.
    joined = urlparse(url).path.lower()
    for slug, name in REGION_SLUG_INDEX.items():
        if slug in joined:
            return name
    return fallback


PRICE_PATTERNS = [
    re.compile(r'"price"\s*:\s*"?R?\s*([\d\s,\.]+)"?'),
    re.compile(r'R\s*([\d][\d\s,\.]{5,15})', re.IGNORECASE),
    re.compile(r'ZAR\s*([\d][\d\s,\.]{5,15})', re.IGNORECASE),
]
BEDS_PATTERNS = [
    re.compile(r'"bedrooms"\s*:\s*"?(\d{1,2})"?'),
    re.compile(r'(\d{1,2})\s*(?:bed|bedroom|bedrooms)\b', re.IGNORECASE),
]


def _parse_int(raw: str) -> Optional[int]:
    cleaned = re.sub(r"[^\d]", "", raw)
    if not cleaned:
        return None
    try:
        value = int(cleaned)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def _extract_price(body: str) -> Optional[int]:
    for pat in PRICE_PATTERNS:
        for match in pat.findall(body):
            val = _parse_int(match)
            if val is not None and 500_000 <= val <= 100_000_000:
                return val
    return None


def _extract_beds(body: str) -> Optional[int]:
    for pat in BEDS_PATTERNS:
        for match in pat.findall(body):
            val = _parse_int(match)
            if val is not None and 1 <= val <= 15:
                return val
    return None


def _enrich_from_detail(state: SearchState, url: str) -> tuple[Optional[int], Optional[int]]:
    resp = _fetch(state, url)
    if resp is None or resp.status_code >= 400:
        return None, None
    body = resp.text
    return _extract_price(body), _extract_beds(body)


def _record_candidate(
    state: SearchState,
    url: str,
    searched_region: str,
    source_domain: str,
) -> bool:
    """Return True if the candidate was added as a new row.
    Performs URL dedup, area inference, detail-page enrichment, and
    budget/bedroom filtering before writing the row.
    """
    key = url.strip().rstrip("/")
    if key in state.existing_urls:
        state.duplicates_skipped += 1
        return False
    state.existing_urls.add(key)

    area = _infer_area(url, searched_region)
    price, beds = _enrich_from_detail(state, url)
    time.sleep(0.3)

    if not _within_budget(state, price, beds):
        return False

    row = {
        "id": "",
        "url": url,
        "address": "unknown",
        "area": area,
        "price": str(price) if price else "0",
        "bedrooms": str(beds) if beds else "unknown",
        "bathrooms": "unknown",
        "garage": "unknown",
        "flatlet": "unknown",
        "garden": "unknown",
        "agent_name": "unknown",
        "agent_phone": "",
        "agent_email": "",
        "listing_status": "active",
        "status": "unseen",
        "notes": (
            f"Auto-added by daily search from {source_domain}. "
            "Metadata extracted from detail page — verify manually."
        ),
        "date_added": TODAY,
        "score": "5",
        "last_checked": TODAY,
        "score_reason": "auto-added, awaiting scoring",
    }
    state.new_rows.append(row)
    return True


# ---------------------------------------------------------------------------
# Source-specific adapters
# ---------------------------------------------------------------------------


def search_privateproperty(state: SearchState, source: str) -> SourceOutcome:
    outcome = SourceOutcome(source=source)
    cfg = state.config
    beds = cfg["bedrooms_min"]
    min_p = cfg["budget"]["min_zar"]
    max_p = cfg["budget"]["max_zar"]
    domain = _domain_of(source)
    started = time.time()

    for region in cfg["regions"]:
        if time.time() - started > PER_SOURCE_MAX_S:
            outcome.outcome = "PARTIAL"
            outcome.error = "per-source time budget exceeded"
            return outcome
        slug = _slug(region)
        url = (
            f"https://www.privateproperty.co.za/for-sale/western-cape/"
            f"cape-town/southern-suburbs/{slug}?bmi={beds}&fp={min_p}&tp={max_p}"
        )
        resp = _fetch(state, url)
        outcome.pages_searched += 1
        if resp is None:
            continue
        if resp.status_code >= 400:
            continue
        listing_urls = re.findall(
            r'href="(/for-sale/western-cape/cape-town/southern-suburbs/[^"]+/T\d+)"',
            resp.text,
        )
        for path in set(listing_urls):
            full = f"https://www.privateproperty.co.za{path}"
            if _record_candidate(state, full, region, domain):
                outcome.new_listings += 1
        time.sleep(PAGE_SLEEP_S)

    outcome.outcome = "SUCCESS" if outcome.new_listings else "NO_RESULTS"
    return outcome


def search_property24(state: SearchState, source: str) -> SourceOutcome:
    outcome = SourceOutcome(source=source)
    domain = _domain_of(source)
    started = time.time()
    reached_any = False
    for region in PRIORITY_REGIONS:
        if time.time() - started > PER_SOURCE_MAX_S:
            outcome.outcome = "PARTIAL"
            return outcome
        slug = _slug(region)
        url = f"https://www.property24.com/for-sale/{slug}/cape-town/western-cape"
        resp = _fetch(state, url)
        outcome.pages_searched += 1
        if resp is None:
            continue
        if resp.status_code in (403, 429, 401):
            outcome.outcome = "FAILED"
            outcome.error = f"HTTP {resp.status_code} (blocked by bot protection)"
            return outcome
        if resp.status_code >= 400:
            continue
        reached_any = True
        listing_paths = re.findall(
            r'href="(/for-sale/[^"]+/\d+)"', resp.text
        )
        for path in set(listing_paths):
            if "/cape-town/western-cape/" not in path:
                continue
            full = f"https://www.property24.com{path}"
            if _record_candidate(state, full, region, domain):
                outcome.new_listings += 1
        time.sleep(PAGE_SLEEP_S)
    if not reached_any:
        outcome.outcome = "FAILED"
        outcome.error = outcome.error or "all pages returned an error"
        return outcome
    outcome.outcome = "SUCCESS" if outcome.new_listings else "NO_RESULTS"
    return outcome


def _generic_pattern_search(
    state: SearchState,
    source: str,
    url_template: Callable[[str], str],
    href_pattern: str,
    absolute_prefix: str,
) -> SourceOutcome:
    outcome = SourceOutcome(source=source)
    domain = _domain_of(source)
    started = time.time()
    reached_any = False
    for region in state.config["regions"]:
        if time.time() - started > PER_SOURCE_MAX_S:
            outcome.outcome = "PARTIAL"
            return outcome
        url = url_template(region)
        resp = _fetch(state, url)
        outcome.pages_searched += 1
        if resp is None:
            continue
        if resp.status_code in (403, 429, 401):
            outcome.outcome = "FAILED"
            outcome.error = f"HTTP {resp.status_code} (blocked)"
            return outcome
        if resp.status_code == 404:
            continue
        if resp.status_code >= 400:
            continue
        reached_any = True
        paths = re.findall(href_pattern, resp.text)
        for path in set(paths):
            full = path if path.startswith("http") else absolute_prefix + path
            if _record_candidate(state, full, region, domain):
                outcome.new_listings += 1
        time.sleep(PAGE_SLEEP_S)
    if not reached_any:
        outcome.outcome = "FAILED"
        outcome.error = outcome.error or "no page returned OK"
        return outcome
    outcome.outcome = "SUCCESS" if outcome.new_listings else "NO_RESULTS"
    return outcome


def search_seeff(state: SearchState, source: str) -> SourceOutcome:
    return _generic_pattern_search(
        state,
        source,
        lambda r: f"https://www.seeff.com/results/residential/for-sale/cape-town/{_slug(r)}/",
        r'href="(/results/residential/for-sale/cape-town/[^"]+/house/\d+/[^"]*)"',
        "https://www.seeff.com",
    )


def search_pamgolding(state: SearchState, source: str) -> SourceOutcome:
    return _generic_pattern_search(
        state,
        source,
        lambda r: f"https://www.pamgolding.co.za/property-search/houses-for-sale-{_slug(r)}-cape-town",
        r'href="(/property-details/[^"?#]+/kw\d+)"',
        "https://www.pamgolding.co.za",
    )


def search_greeff(state: SearchState, source: str) -> SourceOutcome:
    return _generic_pattern_search(
        state,
        source,
        lambda r: f"https://www.greeff.co.za/results/residential/for-sale/cape-town/{_slug(r)}/",
        r'href="(/results/residential/for-sale/cape-town/[^"]+/house/\d+/[^"]*)"',
        "https://www.greeff.co.za",
    )


def search_quay1(state: SearchState, source: str) -> SourceOutcome:
    return _generic_pattern_search(
        state,
        source,
        lambda r: f"https://www.quay1.co.za/results/residential/for-sale/cape-town/{_slug(r)}/",
        r'href="(/results/residential/for-sale/cape-town/[^"]+/house/\d+/[^"]*)"',
        "https://www.quay1.co.za",
    )


def search_jawitz(state: SearchState, source: str) -> SourceOutcome:
    return _generic_pattern_search(
        state,
        source,
        lambda r: f"https://www.jawitz.co.za/results/residential/for-sale/cape-town/{_slug(r)}/",
        r'href="(/results/residential/for-sale/cape-town/[^"]+/house/\d+/[^"]*)"',
        "https://www.jawitz.co.za",
    )


def search_headsproperty(state: SearchState, source: str) -> SourceOutcome:
    return _generic_pattern_search(
        state,
        source,
        lambda r: f"https://www.headsproperty.co.za/results/residential/for-sale/cape-town/{_slug(r)}/",
        r'href="(/results/residential/for-sale/cape-town/[^"]+/house/\d+/[^"]*)"',
        "https://www.headsproperty.co.za",
    )


def search_chaseveritt(state: SearchState, source: str) -> SourceOutcome:
    return _generic_pattern_search(
        state,
        source,
        lambda r: f"https://www.chaseveritt.co.za/results/residential/for-sale/cape-town/{_slug(r)}/",
        r'href="(/results/residential/for-sale/cape-town/[^"]+/house/\d+/[^"]*)"',
        "https://www.chaseveritt.co.za",
    )


def search_remax(state: SearchState, source: str) -> SourceOutcome:
    return _generic_pattern_search(
        state,
        source,
        lambda r: f"https://www.remax.co.za/property-for-sale-in-{_slug(r)}",
        r'href="(https?://www\.remax\.co\.za/property-details/[^"?#]+)"',
        "",
    )


def search_rawson(state: SearchState, source: str) -> SourceOutcome:
    return _generic_pattern_search(
        state,
        source,
        lambda r: f"https://rawson.co.za/for-sale/{_slug(r)}/",
        r'href="(https?://rawson\.co\.za/property/[^"?#]+)"',
        "",
    )


def search_probe_only(state: SearchState, source: str) -> SourceOutcome:
    """Best-effort probe for sources without a known URL pattern.
    We simply check that the site is reachable and record NO_RESULTS if so.
    """
    outcome = SourceOutcome(source=source)
    resp = _fetch(state, source)
    outcome.pages_searched = 1
    if resp is None:
        outcome.outcome = "FAILED"
        outcome.error = "unreachable"
        return outcome
    if resp.status_code >= 400:
        outcome.outcome = "FAILED"
        outcome.error = f"HTTP {resp.status_code}"
        return outcome
    outcome.outcome = "NO_RESULTS"
    outcome.error = "no site-specific search adapter implemented; site reachable but not scraped"
    return outcome


ADAPTERS: dict[str, Callable[[SearchState, str], SourceOutcome]] = {
    "www.property24.com": search_property24,
    "www.privateproperty.co.za": search_privateproperty,
    "www.pamgolding.co.za": search_pamgolding,
    "www.seeff.com": search_seeff,
    "www.greeff.co.za": search_greeff,
    "www.quay1.co.za": search_quay1,
    "www.jawitz.co.za": search_jawitz,
    "www.headsproperty.co.za": search_headsproperty,
    "www.chaseveritt.co.za": search_chaseveritt,
    "www.remax.co.za": search_remax,
    "rawson.co.za": search_rawson,
}


def _pick_adapter(source: str) -> Callable[[SearchState, str], SourceOutcome]:
    dom = _domain_of(source)
    return ADAPTERS.get(dom, search_probe_only)


def _append_new_rows(fieldnames: list[str], existing: list[dict], new_rows: list[dict]) -> tuple[list[str], list[dict]]:
    for extra in ("last_checked", "score_reason"):
        if extra not in fieldnames:
            fieldnames.append(extra)
    max_id = max((int(r["id"]) for r in existing if r.get("id", "").isdigit()), default=0)
    for i, row in enumerate(new_rows, start=1):
        row["id"] = str(max_id + i)
    return fieldnames, existing + new_rows


def _save_rows(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            for col in fieldnames:
                row.setdefault(col, "")
            writer.writerow(row)


def main() -> int:
    config = _load_config()
    _build_region_index(config["regions"])
    fieldnames, existing = _load_existing(CSV_PATH)
    urls, pairs = _index_existing(existing)

    with requests.Session() as session:
        state = SearchState(
            session=session,
            existing_urls=urls,
            existing_addresses=pairs,
            config=config,
        )
        for source in config["sources"]:
            adapter = _pick_adapter(source)
            print(f"[search] {source} → {adapter.__name__}", flush=True)
            try:
                outcome = adapter(state, source)
            except Exception as exc:  # noqa: BLE001
                outcome = SourceOutcome(
                    source=source, outcome="FAILED",
                    error=f"exception: {type(exc).__name__}: {str(exc)[:120]}",
                )
            state.outcomes.append(outcome)
            print(
                f"  → {outcome.outcome} new={outcome.new_listings} pages={outcome.pages_searched}"
                + (f" err={outcome.error}" if outcome.error else ""),
                flush=True,
            )

    fieldnames, all_rows = _append_new_rows(fieldnames, existing, state.new_rows)
    _save_rows(CSV_PATH, fieldnames, all_rows)

    result = {
        "sources_attempted": len(state.outcomes),
        "source_outcomes": [o.__dict__ for o in state.outcomes],
        "new_listings_added": len(state.new_rows),
        "duplicates_skipped": state.duplicates_skipped,
        "duplicates_url_updated": state.duplicates_url_updated,
    }
    OUT_JSON.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
