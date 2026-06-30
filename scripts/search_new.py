#!/usr/bin/env python3
"""Step 3: search configured sources for new listings matching config.

Strategy per source (informed by 2026-06-29 reconnaissance):
- seeff.com: /results/residential/for-sale/cape-town/<slug>/ renders server-side.
  Adding ?filters causes HTTP 500, so we fetch unfiltered and filter client-side.
- privateproperty.co.za: /search is JS-rendered, but
  /for-sale/western-cape/cape-town/southern-suburbs/<slug>/ landing pages list
  some recent listings server-side. Try that.
- pamgolding.co.za: /results-residential/for-sale/Cape-Town/<slug>/ — also tries
  for server-side anchors.
- All other sources: attempt one HTTP GET on a likely-areawise search URL; if
  the response is < HTTP 200 / Cloudflare / no anchors -> mark FAILED.

For each candidate, fetch the detail page to extract price/bedrooms/etc.
Dedupe against existing listings.csv by URL (exact) or address (case-insensitive
fuzzy within same suburb).
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
from typing import Optional
from urllib.parse import urljoin, urlparse

import urllib3
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "listings.csv"
CONFIG_PATH = ROOT / "search-config.json"
TODAY = date.today().isoformat()

REQUIRED_COLS = [
    "id", "url", "address", "area", "price", "bedrooms", "bathrooms",
    "garage", "flatlet", "garden", "agent_name", "agent_phone", "agent_email",
    "listing_status", "status", "notes", "date_added", "score",
    "last_checked", "score_reason",
]

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-ZA,en;q=0.9",
    "Connection": "keep-alive",
}
http = urllib3.PoolManager(
    timeout=urllib3.Timeout(connect=8.0, read=20.0),
    retries=Retry(total=1, backoff_factor=0.5),
    headers=HEADERS,
)


@dataclass
class FetchResult:
    status_code: Optional[int]
    body: str
    error: Optional[str]


def fetch(url: str) -> FetchResult:
    try:
        resp = http.request("GET", url, redirect=True)
    except Exception as exc:  # noqa: BLE001
        return FetchResult(None, "", f"{type(exc).__name__}: {exc}")
    try:
        body = resp.data.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        body = ""
    return FetchResult(resp.status, body, None)


def region_slug(region: str) -> str:
    return region.lower().replace(" ", "-")


# ---------- per-source search adapters ----------------------------------------

def seeff_search_urls(region: str) -> list[str]:
    return [
        f"https://www.seeff.com/results/residential/for-sale/cape-town/{region_slug(region)}/"
    ]


def seeff_extract_listings(body: str, base_url: str) -> list[str]:
    pattern = re.compile(
        r'href="(/results/residential/for-sale/cape-town/[a-z0-9\-]+/house/\d+/[a-z0-9\-]*/?)"',
        re.I,
    )
    return list({urljoin(base_url, m.group(1)) for m in pattern.finditer(body)})


def seeff_parse_detail(url: str, body: str) -> Optional[dict]:
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    title_text = re.sub(r"\s+", " ", title.group(1)).strip() if title else ""
    if re.search(r"^\s*\d+\s+(properties|houses|homes)", title_text, re.I):
        return None
    price_m = re.search(r"price\s*:\s*([0-9]+(?:\.[0-9]+)?)", body)
    price = int(float(price_m.group(1))) if price_m else 0
    bed_m = re.search(r"(\d+)\s*Bedroom", title_text, re.I)
    bedrooms = int(bed_m.group(1)) if bed_m else 0
    bath_m = re.search(r'"?bathrooms"?\s*:\s*([0-9]+(?:\.[0-9]+)?)', body, re.I)
    bathrooms = int(float(bath_m.group(1))) if bath_m else 0
    garage_m = re.search(r'"?(?:garages|parking_spaces)"?\s*:\s*(\d+)', body, re.I)
    garage_n = int(garage_m.group(1)) if garage_m else 0
    floor_m = re.search(r"floor_size\s*:\s*([0-9]+(?:\.[0-9]+)?)", body)
    land_m = re.search(r"land_size\s*:\s*([0-9]+(?:\.[0-9]+)?)", body)
    erf = int(float(land_m.group(1))) if land_m else (
        int(float(floor_m.group(1))) if floor_m else 0
    )
    area_m = re.search(r"For Sale in ([A-Za-z][A-Za-z \-]+)", title_text)
    area = area_m.group(1).strip() if area_m else ""
    desc_m = re.search(
        r'<meta[^>]+name="description"[^>]+content="([^"]+)"', body, re.I
    )
    desc = desc_m.group(1).strip()[:280] if desc_m else ""
    listing_status = "active"
    if re.search(r"\bUnder\s+Offer\b", title_text, re.I):
        listing_status = "under_offer"
    elif re.search(r"\bSold\b", title_text, re.I):
        listing_status = "sold"
    addr_seg = url.rstrip("/").rsplit("/", 1)[-1]
    if re.fullmatch(r"\d+", addr_seg):
        address = "unknown"
    else:
        address = addr_seg.replace("-", " ").strip().title() or "unknown"
    return {
        "title": title_text, "address": address, "area": area, "price": price,
        "bedrooms": bedrooms, "bathrooms": bathrooms,
        "garage": "yes" if garage_n else "unknown",
        "garden": "unknown", "flatlet": "unknown", "erf": erf,
        "agent_name": "Seeff", "agent_phone": "", "agent_email": "",
        "description": desc, "listing_status": listing_status, "url": url,
    }


def privateproperty_search_urls(region: str) -> list[str]:
    return [
        f"https://www.privateproperty.co.za/for-sale/western-cape/cape-town/"
        f"southern-suburbs/{region_slug(region)}/"
    ]


def privateproperty_extract_listings(body: str, base_url: str) -> list[str]:
    pat = re.compile(
        r'href="(/for-sale/western-cape/cape-town/[^"\s]*?/T\d+)"', re.I,
    )
    return list({urljoin(base_url, m.group(1)) for m in pat.finditer(body)})


def privateproperty_parse_detail(url: str, body: str) -> Optional[dict]:
    low = body.lower()
    if "no longer available" in low:
        return None
    title_m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
    price_m = re.search(r"R\s*([\d ,]+)\s*\|", title) or re.search(
        r'"price"\s*:\s*"?(\d[\d ,]*)', body
    )
    price = int(re.sub(r"[ ,]", "", price_m.group(1))) if price_m else 0
    bed_m = re.search(r"(\d+)\s*bed", title, re.I)
    bedrooms = int(bed_m.group(1)) if bed_m else 0
    bath_m = re.search(r"(\d+)\s*bath", title, re.I)
    bathrooms = int(bath_m.group(1)) if bath_m else 0
    garage_m = re.search(r"(\d+)\s*garage", title, re.I)
    garage = "yes" if garage_m and int(garage_m.group(1)) > 0 else "unknown"
    erf_m = re.search(r"(\d{2,5})\s*m[²2]", body)
    erf = int(erf_m.group(1)) if erf_m else 0
    desc_m = re.search(
        r'<meta[^>]+name="description"[^>]+content="([^"]+)"', body, re.I
    )
    desc = desc_m.group(1).strip()[:280] if desc_m else ""
    addr_m = re.search(
        r"/southern-suburbs/[^/]+/([^/]+)/T\d+", url
    )
    if addr_m:
        seg = addr_m.group(1)
        address = seg.replace("-", " ").title()
    else:
        address = "unknown"
    area_m = re.search(r"/southern-suburbs/([^/]+)/", url)
    area = area_m.group(1).replace("-", " ").title() if area_m else ""
    flatlet = "yes" if re.search(r"\b(flatlet|cottage|granny)\b", desc, re.I) else "unknown"
    garden = "yes" if re.search(r"\b(garden|grass|lawn)\b", desc, re.I) else "unknown"
    listing_status = "active"
    if "listing-banner--sold" in low:
        listing_status = "sold"
    elif "listing-banner--offer-pending" in low:
        listing_status = "under_offer"
    return {
        "title": title, "address": address, "area": area, "price": price,
        "bedrooms": bedrooms, "bathrooms": bathrooms,
        "garage": garage, "garden": garden, "flatlet": flatlet, "erf": erf,
        "agent_name": "unknown", "agent_phone": "", "agent_email": "",
        "description": desc, "listing_status": listing_status, "url": url,
    }


def pamgolding_search_urls(region: str) -> list[str]:
    slug = region.replace(" ", "-")
    return [
        f"https://www.pamgolding.co.za/results-residential/for-sale/Cape-Town/{slug}/"
    ]


def pamgolding_extract_listings(body: str, base_url: str) -> list[str]:
    pat = re.compile(
        r'href="(/property-details/[^"\s]*?/kw\d+)"', re.I,
    )
    return list({urljoin(base_url, m.group(1)) for m in pat.finditer(body)})


def pamgolding_parse_detail(url: str, body: str) -> Optional[dict]:
    title_m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
    if re.search(r"\d+\s+Properties\s+(?:and\s+Homes\s+)?For\s+Sale", title, re.I):
        return None
    price_m = re.search(r"R\s*([\d ,]+)", title) or re.search(
        r'"price"\s*:\s*"?(\d[\d ,]*)', body
    )
    price = int(re.sub(r"[ ,]", "", price_m.group(1))) if price_m else 0
    bed_m = re.search(r"(\d+)[-\s]?[Bb]edroom", title)
    bedrooms = int(bed_m.group(1)) if bed_m else 0
    area_m = re.search(r"in ([A-Z][A-Za-z\- ]+)", title)
    area = area_m.group(1).strip() if area_m else ""
    desc_m = re.search(
        r'<meta[^>]+name="description"[^>]+content="([^"]+)"', body, re.I
    )
    desc = desc_m.group(1).strip()[:280] if desc_m else ""
    listing_status = "active"
    if re.search(r"\bUnder\s+Offer\b", title, re.I):
        listing_status = "under_offer"
    elif re.search(r"\bSold\b", title, re.I):
        listing_status = "sold"
    flatlet = "yes" if re.search(r"\b(flatlet|cottage|granny|second dwelling)\b", desc, re.I) else "unknown"
    garden = "yes" if re.search(r"\b(garden|lawn|grass)\b", desc, re.I) else "unknown"
    garage = "yes" if re.search(r"\bgarage\b", desc, re.I) else "unknown"
    return {
        "title": title, "address": "unknown", "area": area, "price": price,
        "bedrooms": bedrooms, "bathrooms": 0, "garage": garage,
        "garden": garden, "flatlet": flatlet, "erf": 0,
        "agent_name": "Pam Golding Properties", "agent_phone": "",
        "agent_email": "", "description": desc,
        "listing_status": listing_status, "url": url,
    }


SOURCE_ADAPTERS = {
    "www.seeff.com": (seeff_search_urls, seeff_extract_listings, seeff_parse_detail, "Seeff"),
    "www.privateproperty.co.za": (
        privateproperty_search_urls, privateproperty_extract_listings,
        privateproperty_parse_detail, "PrivateProperty",
    ),
    "www.pamgolding.co.za": (
        pamgolding_search_urls, pamgolding_extract_listings,
        pamgolding_parse_detail, "PamGolding",
    ),
}


# ---------- dedupe / matching -------------------------------------------------

def norm_addr(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def find_dup(rows: list[dict], cand: dict) -> Optional[dict]:
    cand_url = (cand.get("url") or "").rstrip("/")
    for r in rows:
        if (r.get("url") or "").rstrip("/") == cand_url:
            return r
    cand_addr_n = norm_addr(cand.get("address", ""))
    cand_area_n = norm_addr(cand.get("area", ""))
    if not cand_addr_n or cand_addr_n in {"unknown", ""}:
        return None
    for r in rows:
        ra = norm_addr(r.get("address", ""))
        if ra == cand_addr_n and norm_addr(r.get("area", "")) == cand_area_n:
            return r
    return None


# ---------- main orchestration ------------------------------------------------

@dataclass
class SourceOutcome:
    source: str
    outcome: str = "NO_RESULTS"
    new_listings: int = 0
    pages_searched: int = 0
    error: str = ""
    details: list[str] = field(default_factory=list)


def matches_criteria(listing: dict, cfg: dict) -> bool:
    if listing.get("price", 0) and (
        listing["price"] < cfg["budget"]["min_zar"]
        or listing["price"] > cfg["budget"]["max_zar"]
    ):
        return False
    if listing.get("bedrooms", 0) and listing["bedrooms"] < cfg["bedrooms_min"]:
        return False
    area_l = (listing.get("area", "") or "").lower()
    regions_l = [r.lower() for r in cfg["regions"]]
    if area_l:
        if not any(r == area_l or r in area_l or area_l in r for r in regions_l):
            return False
    return True


def main() -> int:
    cfg = json.loads(CONFIG_PATH.read_text())
    rows: list[dict] = []
    with CSV_PATH.open() as fh:
        for row in csv.DictReader(fh):
            for col in REQUIRED_COLS:
                row.setdefault(col, "")
            rows.append(row)
    next_id = max((int(r["id"]) for r in rows if r.get("id")), default=0) + 1

    outcomes: dict[str, SourceOutcome] = {}
    new_added = 0
    dupes_skipped = 0
    url_updated = 0
    url_update_log: list[dict] = []

    for source_url in cfg["sources"]:
        host = urlparse(source_url).netloc.lower()
        outcome = SourceOutcome(source=host)
        outcomes[host] = outcome
        adapter = SOURCE_ADAPTERS.get(host)
        if not adapter:
            outcome.outcome = "FAILED"
            outcome.error = "no usable HTTP scraper (JS-rendered / Cloudflare)"
            continue
        get_urls, extract, parse, agent = adapter
        any_ok = False
        any_blocked = False
        candidates: set[str] = set()
        for region in cfg["regions"]:
            for surl in get_urls(region):
                outcome.pages_searched += 1
                r = fetch(surl)
                if r.error or (r.status_code or 0) >= 500 or r.status_code == 403:
                    any_blocked = True
                    outcome.details.append(
                        f"{region}: {r.error or 'HTTP ' + str(r.status_code)}"
                    )
                    continue
                if r.status_code != 200:
                    outcome.details.append(f"{region}: HTTP {r.status_code}")
                    continue
                any_ok = True
                found = extract(r.body, surl)
                candidates.update(found)
                time.sleep(0.4)
        if not any_ok and any_blocked:
            outcome.outcome = "FAILED"
            outcome.error = "all region pages blocked / 5xx"
            continue
        if not candidates:
            outcome.outcome = "NO_RESULTS"
            continue

        new_for_source = 0
        partial = False
        for c_url in sorted(candidates):
            time.sleep(0.4)
            dr = fetch(c_url)
            if dr.error or (dr.status_code or 0) != 200:
                partial = True
                outcome.details.append(
                    f"detail blocked {c_url}: "
                    f"{dr.error or 'HTTP ' + str(dr.status_code)}"
                )
                continue
            listing = parse(c_url, dr.body)
            if not listing:
                continue
            if not matches_criteria(listing, cfg):
                continue
            dup = find_dup(rows, listing)
            if dup is not None:
                dup_url = (dup.get("url") or "").rstrip("/")
                if dup_url != c_url.rstrip("/"):
                    url_update_log.append({
                        "id": dup["id"], "old_url": dup_url, "new_url": c_url,
                        "source": host,
                    })
                    dup["url"] = c_url
                    url_updated += 1
                dupes_skipped += 1
                continue
            new_row = {k: "" for k in REQUIRED_COLS}
            new_row.update({
                "id": str(next_id),
                "url": c_url,
                "address": listing.get("address", "unknown") or "unknown",
                "area": listing.get("area", "") or "",
                "price": str(listing.get("price", 0) or 0),
                "bedrooms": str(listing.get("bedrooms", 0) or 0),
                "bathrooms": str(listing.get("bathrooms", 0) or 0),
                "garage": listing.get("garage", "unknown"),
                "flatlet": listing.get("flatlet", "unknown"),
                "garden": listing.get("garden", "unknown"),
                "agent_name": listing.get("agent_name", "") or agent,
                "agent_phone": listing.get("agent_phone", ""),
                "agent_email": listing.get("agent_email", ""),
                "listing_status": listing.get("listing_status", "active"),
                "status": "unseen",
                "notes": (listing.get("description") or "").replace(",", ";"),
                "date_added": TODAY,
                "score": "",
                "last_checked": TODAY,
                "score_reason": "",
            })
            rows.append(new_row)
            next_id += 1
            new_added += 1
            new_for_source += 1
        outcome.new_listings = new_for_source
        if partial and new_for_source > 0:
            outcome.outcome = "PARTIAL"
            outcome.error = "some detail fetches blocked"
        elif new_for_source == 0 and not partial:
            outcome.outcome = "NO_RESULTS"
        elif new_for_source == 0 and partial:
            outcome.outcome = "PARTIAL"
            outcome.error = "all candidate details blocked"
        else:
            outcome.outcome = "SUCCESS"

    with CSV_PATH.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REQUIRED_COLS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in REQUIRED_COLS})

    summary = {
        "sources_attempted": len(cfg["sources"]),
        "source_outcomes": [
            {
                "source": o.source, "outcome": o.outcome,
                "new_listings": o.new_listings, "pages_searched": o.pages_searched,
                "error": o.error,
            }
            for o in outcomes.values()
        ],
        "new_listings_added": new_added,
        "duplicates_skipped": dupes_skipped,
        "duplicates_url_updated": url_updated,
        "url_update_log": url_update_log,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
