#!/usr/bin/env python3
"""Search each configured source for new listings and append to listings.csv.

Design notes:
- Every source is fetched with source-specific URL patterns for each region.
- Only listings whose URL or address doesn't already exist in listings.csv are
  appended, and only when we can confidently extract them from the search
  results HTML.
- Every source records a machine-readable outcome so downstream steps can
  report FAILED / PARTIAL / NO_RESULTS / SUCCESS without silently skipping.
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
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse

import urllib3

urllib3.disable_warnings()

try:
    import requests
except ImportError:
    print("requests missing", file=sys.stderr)
    sys.exit(1)


BASE = Path(__file__).resolve().parent
CSV_PATH = BASE / "listings.csv"
CONFIG_PATH = BASE / "search-config.json"
TODAY = date.today().isoformat()

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-ZA,en;q=0.9",
}
FETCH_TIMEOUT = 20
MAX_PAGES_PER_REGION = 3
SLEEP_BETWEEN_REQUESTS = 0.6


@dataclass
class SourceOutcome:
    source: str
    outcome: str = "NO_RESULTS"
    new_listings: int = 0
    pages_searched: int = 0
    error: str = ""
    candidates: list[str] = field(default_factory=list)


@dataclass
class ListingCandidate:
    url: str
    area: str
    source_domain: str
    title: str = "unknown"


def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return json.load(f)


def load_existing(path: Path) -> tuple[list[dict], list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        return list(reader), fieldnames


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def http_get(session: requests.Session, url: str) -> requests.Response | None:
    try:
        return session.get(
            url,
            headers=HEADERS,
            timeout=FETCH_TIMEOUT,
            allow_redirects=True,
            verify=False,
        )
    except requests.exceptions.RequestException:
        return None


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().replace("www.", "")


# ---------------------------------------------------------------------------
# Per-source search URL builders
# ---------------------------------------------------------------------------

def _property24_urls(region: str, budget_min: int, budget_max: int,
                     bedrooms_min: int) -> list[str]:
    # property24 encodes filters in the query string; sp=pf/pt/br
    base = f"https://www.property24.com/for-sale/{slug(region)}/cape-town/western-cape"
    qs = f"?sp=pf%3D{budget_min}%26pt%3D{budget_max}%26br%3D{bedrooms_min}"
    return [f"{base}{qs}"]


def _privateproperty_urls(region: str, budget_min: int, budget_max: int,
                          bedrooms_min: int) -> list[str]:
    return [
        f"https://www.privateproperty.co.za/for-sale/western-cape/cape-town/"
        f"southern-suburbs/{slug(region)}?bedrooms={bedrooms_min}"
        f"&priceFrom={budget_min}&priceTo={budget_max}"
    ]


def _pamgolding_urls(region: str, budget_min: int, budget_max: int,
                     bedrooms_min: int) -> list[str]:
    return [
        f"https://www.pamgolding.co.za/property-search/"
        f"residential-properties-for-sale-{slug(region)}-cape-town/2"
        f"?price-min={budget_min}&price-max={budget_max}&bedrooms-min={bedrooms_min}"
    ]


def _seeff_urls(region: str, budget_min: int, budget_max: int,
                bedrooms_min: int) -> list[str]:
    return [
        f"https://www.seeff.com/results/residential/for-sale/cape-town/"
        f"{slug(region)}/?priceFrom={budget_min}&priceTo={budget_max}"
        f"&bedrooms={bedrooms_min}"
    ]


def _sothebys_urls(region: str, budget_min: int, budget_max: int,
                   bedrooms_min: int) -> list[str]:
    return [
        f"https://www.sothebysrealty.co.za/for-sale/residential/"
        f"{slug(region)}?priceMin={budget_min}&priceMax={budget_max}"
        f"&bedrooms={bedrooms_min}"
    ]


def _chaseveritt_urls(region: str, budget_min: int, budget_max: int,
                      bedrooms_min: int) -> list[str]:
    return [
        f"https://www.chaseveritt.co.za/results/residential/for-sale/cape-town/"
        f"{slug(region)}/?bedrooms={bedrooms_min}"
        f"&priceFrom={budget_min}&priceTo={budget_max}"
    ]


def _remax_urls(region: str, budget_min: int, budget_max: int,
                bedrooms_min: int) -> list[str]:
    return [
        f"https://www.remax.co.za/property-for-sale/cape-town/{slug(region)}/"
        f"?price_from={budget_min}&price_to={budget_max}&beds={bedrooms_min}"
    ]


def _engelvoelkers_urls(region: str, budget_min: int, budget_max: int,
                        bedrooms_min: int) -> list[str]:
    return [
        f"https://www.engelvoelkers.com/en-za/search/?q={slug(region)}"
        f"&businessArea=residential&priceRangeMin={budget_min}"
        f"&priceRangeMax={budget_max}&numberOfRoomsMin={bedrooms_min}"
    ]


def _rawson_urls(region: str, budget_min: int, budget_max: int,
                 bedrooms_min: int) -> list[str]:
    return [
        f"https://rawson.co.za/property-for-sale-in-{slug(region)}-cape-town/"
        f"?price_from={budget_min}&price_to={budget_max}&bedrooms={bedrooms_min}"
    ]


def _jawitz_urls(region: str, budget_min: int, budget_max: int,
                 bedrooms_min: int) -> list[str]:
    return [
        f"https://www.jawitz.co.za/results/residential/for-sale/cape-town/"
        f"{slug(region)}/?price_from={budget_min}&price_to={budget_max}"
        f"&bedrooms={bedrooms_min}"
    ]


def _greeff_urls(region: str, budget_min: int, budget_max: int,
                 bedrooms_min: int) -> list[str]:
    return [
        f"https://www.greeff.co.za/results/residential/for-sale/cape-town/"
        f"{slug(region)}/?bedrooms={bedrooms_min}"
        f"&priceFrom={budget_min}&priceTo={budget_max}"
    ]


def _dg_urls(region: str, budget_min: int, budget_max: int,
             bedrooms_min: int) -> list[str]:
    return [
        f"https://www.dgproperties.co.za/results/residential/for-sale/"
        f"cape-town/{slug(region)}/?bedrooms={bedrooms_min}"
        f"&priceFrom={budget_min}&priceTo={budget_max}"
    ]


def _quay1_urls(region: str, budget_min: int, budget_max: int,
                bedrooms_min: int) -> list[str]:
    return [
        f"https://www.quay1.co.za/results/residential/for-sale/cape-town/"
        f"{slug(region)}/?bedrooms={bedrooms_min}"
        f"&priceFrom={budget_min}&priceTo={budget_max}"
    ]


def _storey_urls(region: str, budget_min: int, budget_max: int,
                 bedrooms_min: int) -> list[str]:
    return [
        f"https://www.thestorey.co.za/property-for-sale/{slug(region)}/"
        f"?price_min={budget_min}&price_max={budget_max}&bedrooms={bedrooms_min}"
    ]


def _surdo_urls(region: str, budget_min: int, budget_max: int,
                bedrooms_min: int) -> list[str]:
    return [f"https://surdoprop.co.za/search/?area={slug(region)}"]


def _cambier_urls(region: str, budget_min: int, budget_max: int,
                  bedrooms_min: int) -> list[str]:
    return [f"https://www.cambierproperties.com/property/?location={slug(region)}"]


def _heads_urls(region: str, budget_min: int, budget_max: int,
                bedrooms_min: int) -> list[str]:
    return [
        f"https://www.headsproperty.co.za/results/residential/for-sale/"
        f"cape-town/{slug(region)}/?bedrooms={bedrooms_min}"
        f"&priceFrom={budget_min}&priceTo={budget_max}"
    ]


SEARCH_BUILDERS: dict[str, Callable[[str, int, int, int], list[str]]] = {
    "property24.com": _property24_urls,
    "privateproperty.co.za": _privateproperty_urls,
    "pamgolding.co.za": _pamgolding_urls,
    "seeff.com": _seeff_urls,
    "sothebysrealty.co.za": _sothebys_urls,
    "chaseveritt.co.za": _chaseveritt_urls,
    "remax.co.za": _remax_urls,
    "engelvoelkers.com": _engelvoelkers_urls,
    "rawson.co.za": _rawson_urls,
    "jawitz.co.za": _jawitz_urls,
    "greeff.co.za": _greeff_urls,
    "dgproperties.co.za": _dg_urls,
    "quay1.co.za": _quay1_urls,
    "thestorey.co.za": _storey_urls,
    "surdoprop.co.za": _surdo_urls,
    "cambierproperties.com": _cambier_urls,
    "headsproperty.co.za": _heads_urls,
}


# ---------------------------------------------------------------------------
# Candidate listing extraction
# ---------------------------------------------------------------------------

LISTING_HREF_PATTERNS = [
    re.compile(r'href="(/for-sale/[^"]+/\d+)"'),
    re.compile(r'href="(/for-sale/[^"]+/T\d+[^"]*)"'),
    re.compile(r'href="(/property-details/[^"]+)"'),
    re.compile(
        r'href="(/results/residential/for-sale/[^"]+/house/\d+[^"]*)"'
    ),
    re.compile(r'href="(/property/[^"]+/\d+[^"]+)"'),
    re.compile(r'href="(https?://[^"]*/(?:for-sale|property-details|results)/[^"]+)"'),
]


def extract_listing_urls(base_url: str, body: str,
                         region_slugs: set[str] | None = None) -> list[str]:
    found: set[str] = set()
    for rx in LISTING_HREF_PATTERNS:
        for m in rx.finditer(body):
            href = m.group(1)
            abs_url = urljoin(base_url, href)
            path = urlparse(abs_url).path.rstrip("/").lower()
            tail = path.rsplit("/", 1)[-1]
            if not re.match(r"^(t\d+|\d+|kw\d+|as\d+|[\w-]*\d[\w-]*)$", tail):
                continue
            if region_slugs is not None:
                if not any(f"/{s}/" in path + "/" or path.endswith(f"/{s}")
                           or f"-{s}/" in path or f"-{s}-" in path
                           for s in region_slugs):
                    continue
            found.add(abs_url)
    return sorted(found)


def build_dedupe_indices(rows: list[dict]) -> tuple[set[str], set[tuple[str, str]]]:
    urls = {(r.get("url") or "").strip() for r in rows if r.get("url")}
    addr_pairs = set()
    for r in rows:
        addr = (r.get("address") or "").strip().lower()
        area = (r.get("area") or "").strip().lower()
        if addr and addr != "unknown":
            addr_pairs.add((addr, area))
    return urls, addr_pairs


# ---------------------------------------------------------------------------
# Main search flow
# ---------------------------------------------------------------------------

def search_source(session: requests.Session, source: str, regions: list[str],
                  budget_min: int, budget_max: int, bedrooms_min: int,
                  known_urls: set[str]) -> tuple[SourceOutcome, list[ListingCandidate]]:
    dom = domain_of(source)
    outcome = SourceOutcome(source=dom)
    builder = SEARCH_BUILDERS.get(dom)
    if builder is None:
        outcome.outcome = "FAILED"
        outcome.error = "no search builder"
        return outcome, []

    region_slugs = {slug(r) for r in regions}
    candidates: list[ListingCandidate] = []
    seen_urls: set[str] = set()
    pages = 0
    any_ok = False
    any_fail = False
    error_detail = ""

    consecutive_failures = 0
    for region in regions:
        if consecutive_failures >= 3:
            break
        region_hit_ok = False
        for base_url in builder(region, budget_min, budget_max, bedrooms_min):
            for page in range(1, MAX_PAGES_PER_REGION + 1):
                url = base_url
                if page > 1:
                    joiner = "&" if "?" in url else "?"
                    url = f"{url}{joiner}page={page}"
                r = http_get(session, url)
                pages += 1
                if r is None:
                    any_fail = True
                    error_detail = error_detail or "connection error"
                    break
                if r.status_code in (403, 429):
                    any_fail = True
                    error_detail = error_detail or f"http {r.status_code}"
                    break
                if 500 <= r.status_code < 600:
                    any_fail = True
                    error_detail = error_detail or f"http {r.status_code}"
                    break
                if r.status_code == 404:
                    break
                if r.status_code != 200:
                    any_fail = True
                    error_detail = error_detail or f"http {r.status_code}"
                    break
                any_ok = True
                region_hit_ok = True
                links = extract_listing_urls(url, r.text, region_slugs)
                new_here = 0
                for link in links:
                    if link in seen_urls or link in known_urls:
                        continue
                    seen_urls.add(link)
                    candidates.append(
                        ListingCandidate(url=link, area=region, source_domain=dom)
                    )
                    new_here += 1
                if new_here == 0:
                    break
                time.sleep(SLEEP_BETWEEN_REQUESTS)
            time.sleep(SLEEP_BETWEEN_REQUESTS)
        consecutive_failures = 0 if region_hit_ok else consecutive_failures + 1

    outcome.pages_searched = pages
    outcome.candidates = [c.url for c in candidates]
    if any_ok and any_fail:
        outcome.outcome = "PARTIAL"
        outcome.error = error_detail
    elif any_ok:
        outcome.outcome = "SUCCESS" if candidates else "NO_RESULTS"
    else:
        outcome.outcome = "FAILED"
        outcome.error = error_detail or "no responses"
    return outcome, candidates


def enrich_candidate(session: requests.Session, cand: ListingCandidate) -> dict | None:
    """Fetch the detail page and try to extract price/bedrooms/etc.

    Returns None if the page can't be reached at all.
    """
    r = http_get(session, cand.url)
    if r is None or r.status_code != 200:
        return None
    body = r.text
    title_match = re.search(r"<title>([^<]{5,180})</title>", body, re.IGNORECASE)
    title = (title_match.group(1).strip() if title_match else cand.title)[:120]
    price_match = re.search(r"R\s?([1-9](?:[\s.,]?\d){5,8})", body)
    price = 0
    if price_match:
        digits = re.sub(r"\D", "", price_match.group(1))
        if digits:
            price = int(digits)
    beds_match = re.search(r"(\d+)\s*(?:bed|bedroom)", body, re.IGNORECASE)
    bedrooms = int(beds_match.group(1)) if beds_match else 0
    baths_match = re.search(r"(\d+)\s*(?:bath|bathroom)", body, re.IGNORECASE)
    bathrooms = str(baths_match.group(1)) if baths_match else "unknown"
    return {
        "title": title,
        "price": price,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
    }


def next_id(rows: list[dict]) -> int:
    max_id = 0
    for r in rows:
        try:
            max_id = max(max_id, int(r.get("id") or 0))
        except ValueError:
            continue
    return max_id + 1


def main() -> None:
    config = load_config()
    regions: list[str] = config["regions"]
    sources: list[str] = config["sources"]
    budget_min: int = config["budget"]["min_zar"]
    budget_max: int = config["budget"]["max_zar"]
    bedrooms_min: int = config["bedrooms_min"]

    rows, fieldnames = load_existing(CSV_PATH)
    for col in ("last_checked", "score_reason"):
        if col not in fieldnames:
            fieldnames.append(col)
            for row in rows:
                row.setdefault(col, "")

    known_urls, _known_addrs = build_dedupe_indices(rows)
    session = requests.Session()
    all_outcomes: list[SourceOutcome] = []
    added = 0
    dedup_skipped = 0
    next_row_id = next_id(rows)

    for source in sources:
        print(f"SOURCE {source}", flush=True)
        outcome, candidates = search_source(
            session, source, regions, budget_min, budget_max,
            bedrooms_min, known_urls,
        )
        # Cap enrichment to avoid blowing the runtime budget.
        for cand in candidates[:15]:
            if cand.url in known_urls:
                dedup_skipped += 1
                continue
            info = enrich_candidate(session, cand)
            if info is None:
                continue
            price = info["price"]
            if price and not (budget_min <= price <= budget_max):
                continue
            if info["bedrooms"] and info["bedrooms"] < bedrooms_min:
                continue
            new_row = {name: "" for name in fieldnames}
            new_row.update({
                "id": str(next_row_id),
                "url": cand.url,
                "address": "unknown",
                "area": cand.area,
                "price": str(price) if price else "0",
                "bedrooms": str(info["bedrooms"]) if info["bedrooms"] else "unknown",
                "bathrooms": info["bathrooms"],
                "garage": "unknown",
                "flatlet": "unknown",
                "garden": "unknown",
                "agent_name": "unknown",
                "listing_status": "active",
                "status": "unseen",
                "notes": f"Auto-added {TODAY} from {outcome.source} search. "
                         f"Title: {info['title'][:80]}",
                "date_added": TODAY,
                "score": "5",
                "last_checked": TODAY,
                "score_reason": "new; needs enrichment",
            })
            rows.append(new_row)
            known_urls.add(cand.url)
            next_row_id += 1
            outcome.new_listings += 1
            added += 1
            time.sleep(SLEEP_BETWEEN_REQUESTS)
        all_outcomes.append(outcome)
        print(
            f"  outcome={outcome.outcome} pages={outcome.pages_searched} "
            f"new={outcome.new_listings} error={outcome.error}",
            flush=True,
        )

    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "sources_attempted": len(sources),
        "source_outcomes": [
            {
                "source": o.source,
                "outcome": o.outcome,
                "new_listings": o.new_listings,
                "pages_searched": o.pages_searched,
                "error": o.error,
            }
            for o in all_outcomes
        ],
        "new_listings_added": added,
        "duplicates_skipped": dedup_skipped,
        "duplicates_url_updated": 0,
    }
    (BASE / "search_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
