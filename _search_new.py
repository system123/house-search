#!/usr/bin/env python3
"""Search configured sources for NEW listings matching config criteria.

- Skips listings already marked `sold` on the source page (no value in
  tracking sold listings the user has never seen).
- Preserves user-touched rows (status != "unseen") — only `listing_status`
  and `last_checked` may be updated, never `notes`, `status`, `score`, or
  `score_reason`.
- Emits JSON summary to stdout; appends new rows to listings.csv.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from html import unescape
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests
import urllib3

urllib3.disable_warnings()

BASE = Path(__file__).parent
CSV_PATH = BASE / "listings.csv"
CONFIG_PATH = BASE / "search-config.json"
TODAY = dt.date.today().isoformat()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
TIMEOUT = 20

# Don't add listings already marked as these.
EXCLUDE_LISTING_STATUSES = {"sold", "removed"}


@dataclass
class SourceOutcome:
    source: str
    outcome: str = "NO_RESULTS"
    new_listings: int = 0
    pages_searched: int = 0
    error: str = ""
    listings_seen_total: int = 0
    listings_skipped_sold: int = 0
    listings_skipped_filter: int = 0


@dataclass
class Candidate:
    url: str
    address: str
    area: str
    price: int
    bedrooms: int
    bathrooms: str
    listing_id: str
    listing_status: str
    agent_name: str
    notes: str
    source_domain: str


# ---------- helpers ----------

def fetch(session: requests.Session, url: str) -> Optional[requests.Response]:
    try:
        return session.get(url, timeout=TIMEOUT, verify=False, allow_redirects=True)
    except Exception:
        return None


def strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(html))).strip()


def parse_price(text: str) -> int:
    m = re.search(r"R\s*(\d{1,3}(?:[,. ]\d{3}){1,3})(?!\d)", text)
    if not m:
        return 0
    digits = re.sub(r"\D", "", m.group(1))
    if not digits or len(digits) < 6:
        return 0
    val = int(digits)
    return val if 100_000 < val < 100_000_000 else 0


def parse_beds(text: str) -> int:
    m = re.search(r"(\d+)\s*(?:bed|beds|bedroom)\b", text, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def parse_baths(text: str) -> str:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:bath|baths|bathroom)\b", text, re.IGNORECASE)
    return m.group(1) if m else "unknown"


def detect_status(card_text: str) -> str:
    t = card_text.lower()
    if "under offer" in t or "under-offer" in t:
        return "under_offer"
    # The Seeff/Quay1/etc. cards prefix with "Sold" when status is sold.
    if re.search(r"\bsold\b", t[:80]):
        return "sold"
    return "active"


def in_budget(price: int, cfg: dict) -> bool:
    if price <= 0:
        return False  # require price for filtering
    return cfg["budget"]["min_zar"] <= price <= cfg["budget"]["max_zar"]


def listing_id_from_url(url: str) -> str:
    m = re.search(
        r"/(?:house|townhouse|apartment|flat|cluster|property|freehold|"
        r"studio[-_]?apartment|duplex|penthouse)/(\d+)/",
        url,
    )
    if m:
        return f"std:{m.group(1)}"
    m = re.search(r"/property-details/[^/]+/([a-z]+\d+)/?$", url, re.IGNORECASE)
    if m:
        return f"pamg:{m.group(1).lower()}"
    m = re.search(r"/(T\d{5,})/?$", url)
    if m:
        return f"pp:{m.group(1)}"
    m = re.search(r"/(\d{6,})(?:/?$|/[^/]+/?$)", url)
    if m:
        return f"p24:{m.group(1)}"
    return f"url:{url.rstrip('/').lower()}"


def domain_of(url: str) -> str:
    return urlparse(url).netloc


# ---------- generic "property-card-sm" scraper ----------
# Most of the agent-branded white-label property sites (Seeff, Quay1, Greeff,
# Chas Everitt, Jawitz, Heads, etc.) share a server-rendered card pattern:
#     <a/div data-id="N" ... class="...property-card[-sm]..."  href="/results/.../{type}/N/{slug}/">
#         New|Under offer|Sold ?? R<price> N Bedroom <Type> ... N Bed N Bath
# This scraper extracts each card; addresses come from the URL slug.

CARD_RE = re.compile(
    r'(?:<a|<div)[^>]*data-id="(\d+)"[^>]*'
    r'class="[^"]*(?:property-card|listing-card)[^"]*"',
    re.IGNORECASE,
)
HREF_IN_CARD_RE = re.compile(
    r'href="(/results/residential/for-sale/cape-town/([^/]+)/([a-z\-]+)/(\d+)/([^"/]*)/?)"',
    re.IGNORECASE,
)


def parse_card_block(card_block: str, pid: str, source_name: str) -> Optional[Candidate]:
    """Pull price/beds/status from a single card block of HTML."""
    text = strip_tags(card_block)
    price = parse_price(text)
    beds = parse_beds(text)
    status = detect_status(text)
    if not price:
        return None  # cannot trust cards without parseable price
    return Candidate(
        url="",  # filled by caller
        address="",  # filled by caller
        area="",
        price=price,
        bedrooms=beds,
        bathrooms=parse_baths(text),
        listing_id=f"std:{pid}",
        listing_status=status,
        agent_name="",  # filled by caller
        notes=text[:240],
        source_domain=source_name,
    )


def scrape_card_site(
    session: requests.Session,
    cfg: dict,
    regions: list[str],
    source_name: str,
    origin: str,
    agent_name: str,
    url_for_region: Callable[[str], str],
) -> tuple[list[Candidate], SourceOutcome]:
    """Generic scraper for `property-card-sm`-style sites."""
    out = SourceOutcome(source=source_name)
    cands: list[Candidate] = []
    seen_ids: set[str] = set()
    bmin = cfg["bedrooms_min"]
    pages = 0
    pages_with_listings = 0

    for region in regions:
        url = url_for_region(region)
        r = fetch(session, url)
        pages += 1
        if r is None or r.status_code != 200:
            continue
        slug = region.lower().replace(" ", "-")
        # If we were redirected to a parent category, this region has no results
        if slug not in r.url.lower():
            continue

        # Find all cards' anchor positions
        matches = list(CARD_RE.finditer(r.text))
        if not matches:
            continue
        pages_with_listings += 1
        match_positions = [(m.start(), m) for m in matches]
        match_positions.append((len(r.text), None))

        for i in range(len(match_positions) - 1):
            start, m = match_positions[i]
            if m is None:
                continue
            pid = m.group(1)
            # Cap card block to avoid bleeding into related-listings sections
            end = min(match_positions[i + 1][0], start + 8000)
            card_block = r.text[start:end]
            # Find an href within the card that matches this listing id
            href_m = None
            for hm in HREF_IN_CARD_RE.finditer(card_block):
                if hm.group(4) == pid:
                    href_m = hm
                    break
            if href_m is None:
                continue
            full_path, url_suburb, ptype, _hp_id, addr_slug = href_m.groups()
            cand = parse_card_block(card_block, pid, source_name)
            if cand is None:
                continue
            out.listings_seen_total += 1
            if cand.listing_status in EXCLUDE_LISTING_STATUSES:
                out.listings_skipped_sold += 1
                continue
            if not in_budget(cand.price, cfg) or (cand.bedrooms and cand.bedrooms < bmin):
                out.listings_skipped_filter += 1
                continue
            lid = f"std:{pid}"
            if lid in seen_ids:
                continue
            seen_ids.add(lid)
            url_region_norm = url_suburb.replace("-", " ").title()
            area = url_region_norm if url_region_norm in regions else region
            cand.url = origin + full_path
            cand.address = (
                addr_slug.replace("-", " ").title() if addr_slug else "unknown"
            )
            cand.area = area
            cand.agent_name = agent_name
            cand.notes = f"From {source_name} ({region}). {cand.notes}"
            cands.append(cand)

    out.pages_searched = pages
    out.new_listings = len(cands)
    if pages_with_listings == 0:
        out.outcome = "NO_RESULTS"
    elif cands:
        out.outcome = "SUCCESS"
    else:
        out.outcome = "PARTIAL"
        out.error = (
            f"Pages had listings but none matched filters "
            f"(seen={out.listings_seen_total} sold_skipped={out.listings_skipped_sold} "
            f"filter_skipped={out.listings_skipped_filter})"
        )
    return cands, out


def make_card_scraper(source_name: str, origin: str, agent_name: str):
    """Return a scraper closure for a property-card-sm style site."""
    def scraper(session, cfg, regions):
        return scrape_card_site(
            session, cfg, regions, source_name, origin, agent_name,
            lambda region: f"{origin}/results/residential/for-sale/cape-town/"
                           f"{region.lower().replace(' ', '-')}/",
        )
    return scraper


# ---------- Sites that need manual saved-search URLs ----------

def scrape_manual_saved_search(source_url: str, session) -> SourceOutcome:
    """Marker for sources that require a user-provided saved-search URL.

    Pam Golding and a few others render listings via JS or need a
    session-based filter (cf. the Quay1 advanced_search GUID).
    """
    name = domain_of(source_url)
    out = SourceOutcome(source=name)
    r = fetch(session, source_url)
    out.pages_searched = 1
    if r is None:
        out.outcome = "FAILED"
        out.error = "connection_error"
    elif r.status_code in (403, 503, 429):
        out.outcome = "FAILED"
        out.error = f"http_{r.status_code}_blocked"
    elif r.status_code == 200:
        out.outcome = "NO_RESULTS"
        out.error = (
            "needs a user-provided saved-search URL (like Quay1's "
            "?advanced_search=<guid>) — not auto-discoverable"
        )
    else:
        out.outcome = "FAILED"
        out.error = f"http_{r.status_code}"
    return out


# ---------- main ----------

def load_csv() -> tuple[list[dict], list[str]]:
    with CSV_PATH.open() as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def write_csv(rows: list[dict], fieldnames: list[str]) -> None:
    with CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def fuzzy_addr_match(a: str, b: str) -> bool:
    """House-number + street-name word match (no pure-suffix matches)."""
    suffixes = {
        "road", "rd", "street", "st", "avenue", "ave", "drive", "dr",
        "lane", "ln", "way", "place", "pl", "close", "cres", "crescent",
        "boulevard", "blvd", "court", "ct", "circle", "park", "row", "walk",
    }
    if not a or not b or a == "unknown" or b == "unknown":
        return False
    na = re.sub(r"[^a-z0-9 ]", " ", a.lower()).split()
    nb = re.sub(r"[^a-z0-9 ]", " ", b.lower()).split()
    if not na or not nb:
        return False
    if not ({t for t in na if t.isdigit()} & {t for t in nb if t.isdigit()}):
        return False
    words_a = {t for t in na if not t.isdigit() and t not in suffixes}
    words_b = {t for t in nb if not t.isdigit() and t not in suffixes}
    return bool(words_a & words_b)


def main() -> int:
    cfg = json.loads(CONFIG_PATH.read_text())
    regions: list[str] = cfg["regions"]
    sources: list[str] = cfg["sources"]

    rows, fieldnames = load_csv()
    for col in ("last_checked", "score_reason"):
        if col not in fieldnames:
            fieldnames.append(col)
    for r in rows:
        r.setdefault("last_checked", "")
        r.setdefault("score_reason", "")

    existing_listing_ids = set()
    existing_urls = set()
    for r in rows:
        existing_listing_ids.add(listing_id_from_url(r["url"]))
        existing_urls.add(r["url"].strip().rstrip("/"))

    session = requests.Session()
    session.headers.update(HEADERS)

    scrapers: dict[str, Callable] = {
        "seeff.com": make_card_scraper("seeff.com", "https://www.seeff.com", "Seeff"),
        "greeff.co.za": make_card_scraper("greeff.co.za", "https://www.greeff.co.za",
                                          "Greeff Christies International Real Estate"),
        "chaseveritt.co.za": make_card_scraper(
            "chaseveritt.co.za", "https://www.chaseveritt.co.za", "Chas Everitt",
        ),
        "jawitz.co.za": make_card_scraper("jawitz.co.za", "https://www.jawitz.co.za",
                                          "Jawitz Properties"),
        "quay1.co.za": make_card_scraper("quay1.co.za", "https://www.quay1.co.za",
                                          "Quay 1 International Realty"),
        "headsproperty.co.za": make_card_scraper(
            "headsproperty.co.za", "https://www.headsproperty.co.za", "Heads Property",
        ),
    }

    outcomes: list[SourceOutcome] = []
    all_cands: list[Candidate] = []

    for source_url in sources:
        dom = domain_of(source_url)
        key = re.sub(r"^www\.", "", dom)
        if key in scrapers:
            try:
                cs, out = scrapers[key](session, cfg, regions)
                all_cands.extend(cs)
            except Exception as exc:
                out = SourceOutcome(
                    source=key, outcome="FAILED",
                    error=f"exception:{type(exc).__name__}:{exc}",
                )
            outcomes.append(out)
        else:
            outcomes.append(scrape_manual_saved_search(source_url, session))
        time.sleep(0.4)

    # Dedup
    duplicates_skipped = 0
    duplicates_url_updated = 0
    truly_new: list[Candidate] = []
    new_ids: set[str] = set()
    for c in all_cands:
        if c.listing_id in existing_listing_ids:
            duplicates_skipped += 1
            for r in rows:
                if (
                    listing_id_from_url(r["url"]) == c.listing_id
                    and r["url"].rstrip("/") != c.url.rstrip("/")
                ):
                    r["url"] = c.url
                    duplicates_url_updated += 1
                    break
            continue
        if c.listing_id in new_ids or c.url.strip().rstrip("/") in existing_urls:
            duplicates_skipped += 1
            continue
        if c.address and c.address != "unknown":
            existing_match = next(
                (r for r in rows
                 if r.get("area") == c.area and fuzzy_addr_match(c.address, r.get("address", ""))),
                None,
            )
            if existing_match:
                duplicates_skipped += 1
                old_url = existing_match["url"]
                existing_match["url"] = c.url
                duplicates_url_updated += 1
                continue
        truly_new.append(c)
        new_ids.add(c.listing_id)

    next_id = max((int(r["id"]) for r in rows), default=0) + 1
    for c in truly_new:
        row = {fn: "" for fn in fieldnames}
        row.update({
            "id": str(next_id),
            "url": c.url,
            "address": c.address,
            "area": c.area,
            "price": str(c.price),
            "bedrooms": str(c.bedrooms) if c.bedrooms else "unknown",
            "bathrooms": c.bathrooms,
            "garage": "unknown",
            "flatlet": "unknown",
            "garden": "unknown",
            "agent_name": c.agent_name,
            "agent_phone": "",
            "agent_email": "",
            "listing_status": c.listing_status,
            "status": "unseen",
            "notes": c.notes[:500],
            "date_added": TODAY,
            "score": "5",
            "last_checked": TODAY,
            "score_reason": "",
        })
        rows.append(row)
        next_id += 1

    write_csv(rows, fieldnames)

    result = {
        "sources_attempted": len(sources),
        "source_outcomes": [asdict(o) for o in outcomes],
        "new_listings_added": len(truly_new),
        "duplicates_skipped": duplicates_skipped,
        "duplicates_url_updated": duplicates_url_updated,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
