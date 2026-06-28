#!/usr/bin/env python3
"""Scrape myproperty.co.za listings using Playwright + Stealth.

MyProperty.co.za sits behind Vercel's bot challenge ("Security Checkpoint")
which returns HTTP 429 to plain HTTP clients. The challenge is solved
client-side by Playwright with `playwright-stealth` patches applied.

Per-region URL pattern (server-rendered after the challenge clears):
    https://www.myproperty.co.za/en-za/for-sale/property/western-cape/cape-town/<slug>
        ?minPrice=<n>&maxPrice=<m>&currency=ZAR&page=<p>

Each listing-card block contains:
    - href to a /en-za/for-sale/<type>/.../<listing-id> detail page
    - "R N,NNN,NNN" price string
    - "<beds> <baths> <floor>m² <erf>m²" inline summary
    - "On Show" / "Under Offer" / "Sold" banner when applicable
    - human-readable street address line.
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from html import unescape
from pathlib import Path
from typing import Optional

BASE = Path(__file__).parent

CARD_DELIM_RE = re.compile(
    r'class="h-full mp-outer-card transition-all[^"]*"[^>]*data-mp-result-card'
)
CARD_HREF_RE = re.compile(r'href="(/en-za/for-sale/[a-z]+/[^"]+/[^"]+)"')
PRICE_RE = re.compile(r"R\s*(\d{1,3}(?:[,. ]\d{3}){1,3})(?!\d)")
BEDROOMS_RE = re.compile(r"(\d+)\s*Bedroom\b", re.IGNORECASE)
BEDS_INLINE_RE = re.compile(r"(?:^|\s)(\d+)\s+(\d+(?:\.\d+)?)\s+(?:\d+\s*m²|\d+m²)", re.IGNORECASE)
ADDRESS_RE = re.compile(r"([0-9]{1,4}\s+[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,4}),\s*([A-Z][A-Za-z\s\-]+)")
STATUS_BANNER_RE = re.compile(r"\b(On Show|Under Offer|Sold|Pre-Launch|New)\b", re.IGNORECASE)


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
    source_domain: str = "myproperty.co.za"


@dataclass
class SourceOutcome:
    source: str = "myproperty.co.za"
    outcome: str = "NO_RESULTS"
    new_listings: int = 0
    pages_searched: int = 0
    error: str = ""
    listings_seen_total: int = 0
    listings_skipped_sold: int = 0
    listings_skipped_filter: int = 0
    regions_searched: list[str] = field(default_factory=list)


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(html))).strip()


def _parse_price(text: str) -> int:
    m = PRICE_RE.search(text)
    if not m:
        return 0
    digits = re.sub(r"\D", "", m.group(1))
    if not digits or len(digits) < 6:
        return 0
    val = int(digits)
    return val if 100_000 < val < 100_000_000 else 0


def _parse_bedrooms_baths(text: str) -> tuple[int, str]:
    bm = BEDROOMS_RE.search(text)
    beds = int(bm.group(1)) if bm else 0
    bbm = BEDS_INLINE_RE.search(text)
    baths = "unknown"
    if bbm:
        if not beds:
            beds = int(bbm.group(1))
        baths = bbm.group(2)
    return beds, baths


def _detect_status(text: str) -> str:
    """Detect listing status from the FIRST piece of card content.

    MyProperty cards prefix the listing with a banner like "House Sold R …"
    or "Apartment Under Offer R …" before the price. We look in the first
    chunk of plain text after the verbose `class="…"` opening.
    """
    # The first ~200 chars after stripping markup are usually:
    #   "{Type} {Banner?} R{price} R{price} {address} ..."
    head = text[:400].lower()
    if "under offer" in head:
        return "under_offer"
    if re.search(r"\bsold\b", head):
        return "sold"
    return "active"


def _listing_id_from_url(url: str) -> str:
    m = re.search(r"/(?:rl|pc|\d+/[^/]+)?([a-z0-9]{6,})$", url, re.IGNORECASE)
    if m:
        return f"myp:{m.group(1).lower()}"
    return f"url:{url.rstrip('/').lower()}"


def _address_from_url(href: str) -> str:
    """Extract a human-readable address from the URL slug, if present.

    URL forms:
        /en-za/for-sale/<type>/<province>/<city>/<suburb>/<descriptor>/<id>
        where <descriptor> is either '3-bedroom' (generic) or a real
        street address like '22-lincoln-road'.
    """
    parts = href.split("/")
    if len(parts) < 8:
        return "unknown"
    descriptor = parts[-2] if parts[-2] not in {"property", "house", "apartment"} else ""
    if not descriptor or re.match(r"^\d+[-_]bedroom$", descriptor):
        return "unknown"
    return descriptor.replace("-", " ").title()


def _suburb_from_url(href: str) -> str:
    parts = href.split("/")
    if len(parts) >= 7:
        return parts[6].replace("-", " ").title()
    return ""


def _parse_card_block(block: str) -> Optional[Candidate]:
    href_m = CARD_HREF_RE.search(block)
    if not href_m:
        return None
    href = href_m.group(1)
    # Strip the verbose `class="..."` opening so status detection works.
    block_inner = re.sub(
        r'^[^>]+>\s*', "", block, count=1
    )
    plain = _strip(block_inner)
    price = _parse_price(plain)
    if not price:
        return None
    beds, baths = _parse_bedrooms_baths(plain)
    status = _detect_status(plain)
    # Prefer URL-slug address; fall back to "N Street, Suburb" line in text.
    address = _address_from_url(href)
    if address == "unknown":
        addr_m = ADDRESS_RE.search(plain)
        if addr_m:
            address = addr_m.group(1).strip()
    suburb = _suburb_from_url(href)
    notes = re.sub(r"\s+", " ", plain[:280]).strip()
    return Candidate(
        url="",
        address=address,
        area=suburb,
        price=price,
        bedrooms=beds,
        bathrooms=baths,
        listing_id=_listing_id_from_url(href),
        listing_status=status,
        agent_name="",
        notes=notes,
    )


def _slug(region: str) -> str:
    return region.lower().replace(" ", "-")


def scrape(cfg: dict, regions: list[str], origin: str = "https://www.myproperty.co.za",
           max_pages_per_region: int = 5,
           per_page_sleep: float = 2.5) -> tuple[list[Candidate], SourceOutcome]:
    """Drive Playwright + Stealth to fetch the cards for each region."""
    out = SourceOutcome()
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        from playwright_stealth import Stealth  # type: ignore
    except ImportError as exc:
        out.outcome = "FAILED"
        out.error = f"playwright/stealth not installed: {exc}"
        return [], out

    cands: list[Candidate] = []
    seen_ids: set[str] = set()
    pmin = cfg["budget"]["min_zar"]
    pmax = cfg["budget"]["max_zar"]
    bmin = cfg["bedrooms_min"]

    chrome_path = "/usr/local/bin/google-chrome"
    if not Path(chrome_path).exists():
        chrome_path = None

    try:
        with Stealth().use_sync(sync_playwright()) as p:
            launch_args = {
                "headless": False,
                "args": ["--no-sandbox", "--disable-dev-shm-usage"],
            }
            if chrome_path:
                launch_args["executable_path"] = chrome_path
            browser = p.chromium.launch(**launch_args)
            ctx = browser.new_context(
                viewport={"width": 1366, "height": 900},
                locale="en-GB",
            )
            page = ctx.new_page()
            for region in regions:
                slug = _slug(region)
                region_cards_seen = 0
                for page_no in range(1, max_pages_per_region + 1):
                    url = (
                        f"{origin}/en-za/for-sale/property/western-cape/"
                        f"cape-town/{slug}?minPrice={pmin}&maxPrice={pmax}"
                        f"&currency=ZAR&page={page_no}"
                    )
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        continue
                    # Wait for the Vercel challenge to clear and content to render
                    content = ""
                    for attempt in range(6):
                        time.sleep(per_page_sleep)
                        try:
                            content = page.content()
                        except Exception:
                            content = ""
                            continue
                        if (
                            content
                            and "Security Checkpoint" not in content[:1000]
                            and len(content) > 50_000
                        ):
                            break
                    out.pages_searched += 1
                    if "Security Checkpoint" in content[:1000]:
                        out.outcome = "FAILED"
                        out.error = (
                            "Vercel challenge not bypassed — stealth patches "
                            "may need updating"
                        )
                        browser.close()
                        return cands, out
                    if not content:
                        continue

                    # Split into cards
                    positions = [m.start() for m in CARD_DELIM_RE.finditer(content)]
                    if not positions:
                        # No cards on this page — stop paginating
                        break
                    positions.append(len(content))
                    new_in_this_page = 0
                    for i in range(len(positions) - 1):
                        block = content[positions[i]: positions[i + 1]]
                        cand = _parse_card_block(block)
                        if cand is None:
                            continue
                        # The cards on a /property/.../<suburb> page include
                        # neighbouring suburbs as suggestions; keep only the
                        # ones whose URL contains the suburb slug.
                        if f"/{slug}/" not in (
                            CARD_HREF_RE.search(block).group(1) if CARD_HREF_RE.search(block) else ""
                        ):
                            continue
                        out.listings_seen_total += 1
                        if cand.listing_status in {"sold", "removed"}:
                            out.listings_skipped_sold += 1
                            continue
                        if not (pmin <= cand.price <= pmax):
                            out.listings_skipped_filter += 1
                            continue
                        if cand.bedrooms and cand.bedrooms < bmin:
                            out.listings_skipped_filter += 1
                            continue
                        if cand.listing_id in seen_ids:
                            continue
                        seen_ids.add(cand.listing_id)
                        href_m = CARD_HREF_RE.search(block)
                        cand.url = origin + href_m.group(1)
                        cand.area = region
                        cand.agent_name = "MyProperty.co.za listing"
                        cand.notes = (
                            f"From myproperty.co.za ({region} page {page_no}). "
                            + cand.notes
                        )
                        cands.append(cand)
                        region_cards_seen += 1
                        new_in_this_page += 1
                    if new_in_this_page == 0:
                        break
                out.regions_searched.append(region)
            browser.close()
    except Exception as exc:
        out.outcome = "FAILED"
        out.error = f"playwright_runtime:{type(exc).__name__}:{exc}"
        return cands, out

    out.new_listings = len(cands)
    out.outcome = "SUCCESS" if cands else "NO_RESULTS"
    return cands, out


if __name__ == "__main__":
    cfg = json.loads((BASE / "search-config.json").read_text())
    cands, out = scrape(cfg, cfg["regions"])
    print(json.dumps({
        "outcome": asdict(out),
        "candidates": [
            {**asdict(c), "url": c.url[:120]} for c in cands[:5]
        ],
        "total": len(cands),
    }, indent=2))
