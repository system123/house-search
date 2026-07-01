#!/usr/bin/env python3
"""Step 3 — search configured sources for new listings and append them.

Reality check (see automation memory):
- www.seeff.com area landing pages render server-side and can be paginated.
- Everything else in the configured source list is JS-only or Cloudflare-blocked
  when fetched over plain HTTP, so it is recorded as FAILED for this run.

Outputs a JSON summary on stdout for the run-log to consume.
"""
from __future__ import annotations

import concurrent.futures as futures
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from common import (
    CONFIG_PATH, TODAY, domain_of, http_get, read_listings, write_listings,
)

MAX_PAGES_PER_REGION: int = 5
MAX_WORKERS: int = 6


@dataclass
class Listing:
    """Normalised listing extracted from a source."""
    url: str
    address: str
    area: str
    price: int
    bedrooms: int
    bathrooms: int
    garage: str
    garden: str
    flatlet: str
    erf_size: int
    agent_name: str
    description: str


@dataclass
class SourceOutcome:
    """Per-source result of the search step."""
    source: str
    outcome: str  # SUCCESS | PARTIAL | NO_RESULTS | FAILED
    new_listings: int = 0
    pages_searched: int = 0
    error: str = ""


def load_config() -> dict[str, object]:
    return json.loads(CONFIG_PATH.read_text())


def _slugify_region(region: str) -> str:
    return region.lower().replace(" ", "-")


def _collect_seeff_ids(region: str) -> tuple[set[str], int]:
    """Return (listing_ids, pages_fetched) for a Seeff region."""
    seen: set[str] = set()
    pages = 0
    prev_size = -1
    for page in range(1, MAX_PAGES_PER_REGION + 1):
        suffix = "" if page == 1 else f"?page={page}"
        url = f"https://www.seeff.com/results/residential/for-sale/cape-town/{_slugify_region(region)}/{suffix}"
        fetch = http_get(url, timeout=20)
        pages += 1
        if not fetch.ok:
            break
        ids = set(re.findall(r"/house/(\d{4,})/", fetch.text))
        if not ids or ids <= seen:
            seen |= ids
            break
        if len(seen) == prev_size:
            break
        prev_size = len(seen)
        seen |= ids
    return seen, pages


def _parse_seeff_detail(listing_id: str, region: str) -> Listing | None:
    """Fetch and parse a Seeff detail page. Returns None on failure."""
    slug = _slugify_region(region)
    url = f"https://www.seeff.com/results/residential/for-sale/cape-town/{slug}/house/{listing_id}/"
    fetch = http_get(url, timeout=20)
    if not fetch.ok:
        return None
    body = fetch.text
    final_url = fetch.url_final or url
    title_match = re.search(r"<title[^>]*>([^<]*)</title>", body, re.IGNORECASE)
    title = (title_match.group(1) if title_match else "").strip()
    if not title or re.match(r"\d+\s+(properties|houses|homes)\b", title, re.IGNORECASE):
        return None
    if re.search(r"\bSold\b", title):
        return None
    if re.search(r"\bUnder Offer\b", title, re.IGNORECASE):
        return None  # only surface currently active listings
    price = _first_int(re.search(r"price:\s*(\d+)", body))
    erf = _first_int(re.search(r"land_size:\s*(\d+)", body))
    beds = _first_int(re.search(r">\s*(\d+)\s+Bedrooms?\b", body))
    baths = _first_int(re.search(r">\s*(\d+)\s+Bathrooms?\b", body))
    garages = _first_int(re.search(r">\s*(\d+)\s+Garages?\b", body))
    if not beds:
        beds = _first_int(re.match(r"(\d+)\s+Bedroom", title))
    area = _canonical_area_from_url(final_url) or _extract_seeff_area(title, region)
    address = _extract_seeff_address(final_url) or "unknown"
    description = _extract_meta_description(body)[:300]
    return Listing(
        url=final_url, address=address, area=area, price=price,
        bedrooms=beds, bathrooms=baths,
        garage="yes" if garages > 0 else "unknown",
        garden="unknown", flatlet="unknown", erf_size=erf,
        agent_name="Seeff", description=description,
    )


def _first_int(match: re.Match[str] | None) -> int:
    return int(match.group(1)) if match else 0


def _extract_seeff_area(title: str, region: str) -> str:
    match = re.search(r"in ([^|]+?)\s*\|", title)
    if match:
        return match.group(1).strip()
    return region


def _canonical_area_from_url(final_url: str) -> str:
    """Extract the suburb slug that Seeff canonicalised the URL to."""
    match = re.search(r"/cape-town/([^/]+)/house/", final_url)
    if not match:
        return ""
    return match.group(1).replace("-", " ").title()


def _extract_seeff_address(final_url: str) -> str:
    match = re.search(r"/house/\d+/([^/]+)/?$", final_url)
    if not match:
        return ""
    return match.group(1).replace("-", " ").strip().title()


def _extract_meta_description(body: str) -> str:
    match = re.search(
        r'<meta[^>]*name="description"[^>]*content="([^"]*)"', body, re.IGNORECASE
    )
    return (match.group(1) if match else "").strip()


def _within_criteria(listing: Listing, cfg: dict[str, object]) -> bool:
    budget = cfg["budget"]  # type: ignore[index]
    lo = int(budget["min_zar"])  # type: ignore[index]
    hi = int(budget["max_zar"])  # type: ignore[index]
    if listing.price and not (lo <= listing.price <= hi):
        return False
    if listing.bedrooms and listing.bedrooms < int(cfg["bedrooms_min"]):
        return False
    allowed = {r.lower() for r in cfg["regions"]}  # type: ignore[union-attr]
    if listing.area and listing.area.lower() not in allowed:
        return False
    return True


def search_seeff(cfg: dict[str, object]) -> tuple[list[Listing], SourceOutcome]:
    """Search every configured region on Seeff and return matched listings."""
    regions: list[str] = list(cfg["regions"])  # type: ignore[arg-type]
    total_pages = 0
    listings: list[Listing] = []
    all_ids: list[tuple[str, str]] = []
    for region in regions:
        ids, pages = _collect_seeff_ids(region)
        total_pages += pages
        for lid in ids:
            all_ids.append((lid, region))
    unique_ids: dict[str, str] = {}  # listing_id -> first region that saw it
    for lid, region in all_ids:
        unique_ids.setdefault(lid, region)
    with futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(lambda pair: _parse_seeff_detail(*pair), unique_ids.items()))
    for parsed in results:
        if parsed and _within_criteria(parsed, cfg):
            listings.append(parsed)
    outcome = SourceOutcome(
        source="www.seeff.com",
        outcome="SUCCESS" if listings else "NO_RESULTS",
        new_listings=len(listings),  # updated later after dedup
        pages_searched=total_pages,
    )
    return listings, outcome


def _canonicalise_url(url: str) -> str:
    """Strip trailing slash and lower host for URL equality checks."""
    return url.rstrip("/").split("#", 1)[0].lower()


def _normalise_address(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


@dataclass
class DedupStats:
    """Aggregated dedup counters for the run summary."""
    duplicates_skipped: int = 0
    duplicates_url_updated: int = 0
    url_updates: list[str] = field(default_factory=list)


def _dedupe_and_append(
    listings: list[Listing],
    rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], DedupStats]:
    """Filter *listings* against *rows*, then return new rows + stats."""
    stats = DedupStats()
    known_urls = {_canonicalise_url(r.get("url", "")): r for r in rows if r.get("url")}
    known_by_address: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        addr = _normalise_address(row.get("address", ""))
        area = _normalise_address(row.get("area", ""))
        if addr and addr != "unknown" and area:
            known_by_address[(addr, area)] = row
    next_id = 1 + max((int(r.get("id") or 0) for r in rows), default=0)
    new_rows: list[dict[str, str]] = []
    for listing in listings:
        curl = _canonicalise_url(listing.url)
        if curl in known_urls:
            stats.duplicates_skipped += 1
            continue
        addr_key = (_normalise_address(listing.address), _normalise_address(listing.area))
        if addr_key[0] and addr_key[0] != "unknown" and addr_key in known_by_address:
            existing = known_by_address[addr_key]
            if _canonicalise_url(existing.get("url", "")) == curl:
                stats.duplicates_skipped += 1
            else:
                stats.duplicates_url_updated += 1
                stats.url_updates.append(f"id={existing.get('id')} url<-{listing.url}")
                existing["url"] = listing.url
                known_urls[curl] = existing
            continue
        new_row = _listing_to_row(listing, next_id)
        new_rows.append(new_row)
        known_urls[curl] = new_row
        if addr_key[0] and addr_key[0] != "unknown":
            known_by_address[addr_key] = new_row
        next_id += 1
    return new_rows, stats


def _listing_to_row(listing: Listing, new_id: int) -> dict[str, str]:
    """Convert a Listing dataclass into a listings.csv row dict."""
    return {
        "id": str(new_id),
        "url": listing.url,
        "address": listing.address or "unknown",
        "area": listing.area,
        "price": str(listing.price) if listing.price else "0",
        "bedrooms": str(listing.bedrooms) if listing.bedrooms else "unknown",
        "bathrooms": str(listing.bathrooms) if listing.bathrooms else "unknown",
        "garage": listing.garage,
        "flatlet": listing.flatlet,
        "garden": listing.garden,
        "agent_name": listing.agent_name,
        "agent_phone": "",
        "agent_email": "",
        "listing_status": "active",
        "status": "unseen",
        "notes": (listing.description or "").replace(",", ";").replace("\n", " ")[:300],
        "date_added": TODAY,
        "score": "5",
        "last_checked": TODAY,
        "score_reason": "",
    }


FAILED_REASON = "no usable HTTP scraper (JS-rendered / Cloudflare)"


def _failed_outcome(source_url: str) -> SourceOutcome:
    return SourceOutcome(source=domain_of(source_url), outcome="FAILED", error=FAILED_REASON)


def run() -> dict[str, object]:
    cfg = load_config()
    _rows_headers, rows = read_listings()
    outcomes: list[SourceOutcome] = []
    seeff_listings, seeff_outcome = search_seeff(cfg)
    outcomes.append(seeff_outcome)
    all_new: list[Listing] = list(seeff_listings)
    for source_url in cfg["sources"]:  # type: ignore[union-attr]
        if domain_of(source_url) == "seeff.com":
            continue
        outcomes.append(_failed_outcome(source_url))
    new_rows, stats = _dedupe_and_append(all_new, rows)
    seeff_outcome.new_listings = sum(1 for r in new_rows if "seeff.com" in r["url"])
    rows.extend(new_rows)
    write_listings(rows)
    return _summarise(outcomes, len(new_rows), stats)


def _summarise(
    outcomes: Iterable[SourceOutcome],
    new_added: int,
    stats: DedupStats,
) -> dict[str, object]:
    outs = list(outcomes)
    return {
        "sources_attempted": len(outs),
        "source_outcomes": [o.__dict__ for o in outs],
        "new_listings_added": new_added,
        "duplicates_skipped": stats.duplicates_skipped,
        "duplicates_url_updated": stats.duplicates_url_updated,
        "url_updates": stats.url_updates,
    }


def main() -> int:
    summary = run()
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
