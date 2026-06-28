#!/usr/bin/env python3
"""Search configured sources for new listings matching config criteria.

Emits JSON summary to stdout. Updates listings.csv with genuinely new rows.
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


# ---------- Data classes ----------

@dataclass
class SourceOutcome:
    source: str
    outcome: str = "NO_RESULTS"
    new_listings: int = 0
    pages_searched: int = 0
    error: str = ""


@dataclass
class Candidate:
    url: str
    address: str
    area: str
    price: int
    bedrooms: int
    bathrooms: str
    listing_id: str  # for cross-subdomain dedup
    listing_status: str  # active|under_offer|sold|unknown
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
    # Match South African ZAR price: e.g. "R3,500,000" / "R3 500 000" / "R3.500.000".
    # Require digit groups of 3 to avoid bleeding into trailing numbers.
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
    m = re.search(r"(\d+)\s*(?:bath|baths|bathroom)\b", text, re.IGNORECASE)
    return m.group(1) if m else "unknown"


def detect_status(card_text: str) -> str:
    t = card_text.lower()
    if "under offer" in t or "under-offer" in t:
        return "under_offer"
    if re.search(r"\bsold\b", t):
        return "sold"
    return "active"


def in_budget(price: int, cfg: dict) -> bool:
    if price <= 0:
        return True
    return cfg["budget"]["min_zar"] <= price <= cfg["budget"]["max_zar"]


def listing_id_from_url(url: str) -> str:
    """Extract a listing identifier from URL for cross-subdomain dedup."""
    m = re.search(r"/(?:house|townhouse|apartment|flat|cluster|property)/(\d+)/", url)
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


# ---------- per-source scrapers ----------

def scrape_seeff(session, cfg, regions) -> tuple[list[Candidate], SourceOutcome]:
    out = SourceOutcome(source="seeff.com")
    cands: list[Candidate] = []
    pages = 0
    seen_ids: set[str] = set()
    bmin = cfg["bedrooms_min"]
    MAX_CARD_BYTES = 8000

    for region in regions:
        slug = region.lower().replace(" ", "-")
        url = f"https://www.seeff.com/results/residential/for-sale/cape-town/{slug}/"
        r = fetch(session, url)
        pages += 1
        if r is None or r.status_code != 200 or slug not in r.url.lower():
            continue
        positions = [
            (m.start(), m.group(1))
            for m in re.finditer(
                r'<div[^>]*data-id="(\d+)"[^>]*class="seeff-listing-card">',
                r.text,
            )
        ]
        if not positions:
            continue
        positions.append((len(r.text), ""))
        for i in range(len(positions) - 1):
            start, pid = positions[i]
            # Cap card block size to avoid bleeding into "similar listings" sections
            end = min(positions[i + 1][0], start + MAX_CARD_BYTES)
            block = r.text[start:end]
            href_m = re.search(
                r'href="(/results/residential/for-sale/cape-town/([^/]+)/house/(\d+)/([^"]+?)/?)"',
                block,
            )
            if not href_m:
                continue
            full_path, url_suburb, url_pid, url_slug = (
                href_m.group(1), href_m.group(2), href_m.group(3), href_m.group(4)
            )
            # The href inside the card must match the card's data-id, otherwise
            # we picked up the wrong link (e.g. a related-listing CTA).
            if url_pid != pid:
                continue
            href = "https://www.seeff.com" + full_path
            text = strip_tags(block)
            price = parse_price(text)
            beds = parse_beds(text)
            if not in_budget(price, cfg):
                continue
            if beds and beds < bmin:
                continue
            address = url_slug.replace("-", " ").title() if url_slug else "unknown"
            # Use the suburb from URL (canonical) and map back to a config region if possible
            url_region_norm = url_suburb.replace("-", " ").title()
            area = url_region_norm if url_region_norm in regions else region
            lid = f"std:{pid}"
            if lid in seen_ids:
                continue
            seen_ids.add(lid)
            cands.append(
                Candidate(
                    url=href,
                    address=address,
                    area=area,
                    price=price,
                    bedrooms=beds,
                    bathrooms=parse_baths(text),
                    listing_id=lid,
                    listing_status=detect_status(text),
                    agent_name="Seeff",
                    notes=f"From Seeff search ({region}). {text[:240]}",
                    source_domain="seeff.com",
                )
            )
    out.pages_searched = pages
    out.new_listings = len(cands)
    out.outcome = "SUCCESS" if cands else "NO_RESULTS"
    return cands, out


def _scrape_white_label(
    session, cfg, regions, source_name: str, origin: str
) -> tuple[list[Candidate], SourceOutcome]:
    """Probe a white-label property site for reachability per region.

    These sites (Greeff, Chas Everitt, Jawitz, Quay1, Heads Property) share a
    similar URL pattern but render listing card details client-side via JS.
    Static HTML parsing yields unreliable price/bedroom values, so this
    routine only confirms reachability and returns PARTIAL when listings are
    visible but cannot be parsed reliably for our criteria.
    """
    out = SourceOutcome(source=source_name)
    pages = 0
    pages_with_cards = 0
    pages_redirected = 0
    bmin = cfg["bedrooms_min"]
    for region in regions:
        slug = region.lower().replace(" ", "-")
        url = f"{origin}/results/residential/for-sale/cape-town/{slug}/"
        r = fetch(session, url)
        pages += 1
        if r is None or r.status_code != 200:
            continue
        if slug not in r.url.lower():
            pages_redirected += 1
            continue
        # Count unique listing-id-style hrefs as a signal that cards rendered
        unique = set(re.findall(r"/house/(\d+)/", r.text))
        if unique:
            pages_with_cards += 1
    out.pages_searched = pages
    if pages_with_cards == 0:
        out.outcome = "NO_RESULTS"
        out.error = (
            f"redirected={pages_redirected}; no per-region card markup parseable "
            f"({source_name} renders listing detail client-side)"
        )
    else:
        out.outcome = "PARTIAL"
        out.error = (
            f"{pages_with_cards} pages had listings but price/beds not in "
            f"static HTML — manual review required for {source_name}"
        )
    return [], out


def scrape_greeff(session, cfg, regions):
    return _scrape_white_label(session, cfg, regions, "greeff.co.za", "https://www.greeff.co.za")


def scrape_chaseveritt(session, cfg, regions):
    return _scrape_white_label(session, cfg, regions, "chaseveritt.co.za", "https://www.chaseveritt.co.za")


def scrape_jawitz(session, cfg, regions):
    return _scrape_white_label(session, cfg, regions, "jawitz.co.za", "https://www.jawitz.co.za")


def scrape_quay1(session, cfg, regions):
    return _scrape_white_label(session, cfg, regions, "quay1.co.za", "https://www.quay1.co.za")


def scrape_heads(session, cfg, regions):
    return _scrape_white_label(session, cfg, regions, "headsproperty.co.za", "https://www.headsproperty.co.za")


def scrape_pamgolding(session, cfg, regions) -> tuple[list[Candidate], SourceOutcome]:
    """Probe Pam Golding per region. Their cards are JS-rendered, so this
    only confirms reachability and reports unique KW listing IDs visible."""
    out = SourceOutcome(source="pamgolding.co.za")
    slugs = {
        "Pinelands": ("pinelands-cape-town", 1183),
        "Rondebosch": ("rondebosch-cape-town", 1234),
        "Claremont": ("claremont-cape-town", 1192),
        "Newlands": ("newlands-cape-town", 1219),
        "Bishopscourt": ("bishopscourt-cape-town", 1185),
        "Kenilworth": ("kenilworth-cape-town", 1210),
        "Wynberg": ("wynberg-cape-town", 1265),
        "Plumstead": ("plumstead-cape-town", 1228),
        "Bergvliet": ("bergvliet-cape-town", 1184),
        "Meadowridge": ("meadowridge-cape-town", 1216),
        "Tokai": ("tokai-cape-town", 1258),
        "Constantia": ("constantia-cape-town", 1195),
        "Mowbray": ("mowbray-cape-town", 1218),
        "Rosebank": ("rosebank-cape-town", 1239),
        "Observatory": ("observatory-cape-town", 1224),
        "Lakeside": ("lakeside-cape-town", 1213),
        "Muizenberg": ("muizenberg-cape-town", 1219),
        "Kalk Bay": ("kalk-bay-cape-town", 1209),
        "Gardens": ("gardens-cape-town", 1204),
        "Oranjezicht": ("oranjezicht-cape-town", 1225),
        "Vredehoek": ("vredehoek-cape-town", 1262),
        "De Waterkant": ("de-waterkant-cape-town", 1198),
    }
    pmin = cfg["budget"]["min_zar"]
    pmax = cfg["budget"]["max_zar"]
    bmin = cfg["bedrooms_min"]
    pages = 0
    pages_with_listings = 0
    for region in regions:
        if region not in slugs:
            continue
        slug, sid = slugs[region]
        url = (
            f"https://www.pamgolding.co.za/property-search/properties-for-sale-{slug}/"
            f"{sid}?minPrice={pmin}&maxPrice={pmax}&minBedrooms={bmin}"
        )
        r = fetch(session, url)
        pages += 1
        if r is None or r.status_code != 200:
            continue
        kws = set(re.findall(r"/property-details/[^/\"]+/(kw\d+)", r.text, re.IGNORECASE))
        if kws:
            pages_with_listings += 1
    out.pages_searched = pages
    if pages_with_listings == 0:
        out.outcome = "NO_RESULTS"
    else:
        out.outcome = "PARTIAL"
        out.error = (
            f"{pages_with_listings} pages had listings; prices/beds are "
            "JS-rendered, so cannot verify budget/bedroom criteria reliably"
        )
    return [], out


def scrape_remax(session, cfg, regions) -> tuple[list[Candidate], SourceOutcome]:
    """RE/MAX SPA — listings load via JS; static HTML yields no parseable cards."""
    out = SourceOutcome(source="remax.co.za")
    pages = 0
    bmin = cfg["bedrooms_min"]
    pmin = cfg["budget"]["min_zar"]
    pmax = cfg["budget"]["max_zar"]
    for region in regions:
        slug = region.replace(" ", "-")
        url = (
            f"https://www.remax.co.za/properties-for-sale/{slug}-Cape-Town-Western-Cape/"
            f"?priceFrom={pmin}&priceTo={pmax}&bedroomsFrom={bmin}"
        )
        r = fetch(session, url)
        pages += 1
    out.pages_searched = pages
    out.outcome = "NO_RESULTS"
    out.error = "remax.co.za is a JS-rendered SPA; static HTML has no listing cards"
    return [], out


def scrape_blocked(source_url: str, session) -> SourceOutcome:
    name = urlparse(source_url).netloc
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
        out.error = "no regional search URL pattern available"
    else:
        out.outcome = "FAILED"
        out.error = f"http_{r.status_code}"
    return out


# ---------- main ----------

def load_csv():
    with CSV_PATH.open() as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def write_csv(rows, fieldnames):
    with CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    cfg = json.loads(CONFIG_PATH.read_text())
    regions = cfg["regions"]
    sources = cfg["sources"]

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

    scrapers = {
        "seeff.com": scrape_seeff,
        "greeff.co.za": scrape_greeff,
        "chaseveritt.co.za": scrape_chaseveritt,
        "jawitz.co.za": scrape_jawitz,
        "quay1.co.za": scrape_quay1,
        "headsproperty.co.za": scrape_heads,
        "pamgolding.co.za": scrape_pamgolding,
        "remax.co.za": scrape_remax,
    }

    outcomes: list[SourceOutcome] = []
    all_cands: list[Candidate] = []

    for source_url in sources:
        dom = urlparse(source_url).netloc
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
            outcomes.append(scrape_blocked(source_url, session))
        time.sleep(0.4)

    # Deduplicate candidates against existing rows and each other
    duplicates_skipped = 0
    duplicates_url_updated = 0
    truly_new: list[Candidate] = []
    new_ids: set[str] = set()

    STREET_SUFFIXES = {
        "road", "rd", "street", "st", "avenue", "ave", "drive", "dr",
        "lane", "ln", "way", "place", "pl", "close", "cres", "crescent",
        "boulevard", "blvd", "court", "ct", "circle", "park", "row",
    }

    def fuzzy_addr_match(a: str, b: str) -> bool:
        """Match two addresses if they share the house number AND a meaningful
        street-name token (excluding pure suffixes like "road"/"avenue")."""
        if not a or not b or a == "unknown" or b == "unknown":
            return False
        na = re.sub(r"[^a-z0-9 ]", " ", a.lower()).split()
        nb = re.sub(r"[^a-z0-9 ]", " ", b.lower()).split()
        if not na or not nb:
            return False
        nums_a = {t for t in na if t.isdigit()}
        nums_b = {t for t in nb if t.isdigit()}
        if not (nums_a & nums_b):
            return False
        words_a = {t for t in na if not t.isdigit() and t not in STREET_SUFFIXES}
        words_b = {t for t in nb if not t.isdigit() and t not in STREET_SUFFIXES}
        if not words_a or not words_b:
            return False
        return bool(words_a & words_b)

    for c in all_cands:
        # 1. listing_id match against existing rows -> update URL
        if c.listing_id in existing_listing_ids and c.listing_id not in new_ids:
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
        # 2. Duplicate within this run
        if c.listing_id in new_ids:
            duplicates_skipped += 1
            continue
        # 3. URL match
        if c.url.strip().rstrip("/") in existing_urls:
            duplicates_skipped += 1
            continue
        # 4. Fuzzy address match in same area (cross-agent dual listing)
        cand_addr = c.address
        matched_row = None
        if cand_addr and cand_addr != "unknown":
            for r in rows:
                if r.get("area") == c.area and fuzzy_addr_match(cand_addr, r.get("address", "")):
                    matched_row = r
                    break
        if matched_row:
            duplicates_skipped += 1
            old_url = matched_row["url"]
            matched_row["url"] = c.url
            existing_note = matched_row.get("notes", "") or ""
            extra = f"Cross-agent dual listing: replaced URL on {TODAY} (was {old_url[:80]})"
            if extra not in existing_note:
                matched_row["notes"] = (existing_note + " | " + extra).strip(" |") if existing_note else extra
            duplicates_url_updated += 1
            continue
        truly_new.append(c)
        new_ids.add(c.listing_id)

    # Append
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
