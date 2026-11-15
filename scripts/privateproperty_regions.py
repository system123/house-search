#!/usr/bin/env python3
"""Scrape privateproperty.co.za per-region listing pages.

Discovered 2026-09-20; region path codes stored inline. Complements
`search_sources.py` (which returns NO_RESULTS for privateproperty).
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.privateproperty.co.za"
LISTING_PATH_TEMPLATE = (
    "/for-sale/western-cape/cape-town/southern-suburbs/{slug}/{code}"
)

REGION_CODES: dict[str, str] = {
    "Wynberg": "wynberg/952",
    "Wynberg Upper": "wynberg/wynberg-upper/5990",
    "Kenilworth": "kenilworth/801",
    "Kenilworth Upper": "kenilworth/kenilworth-upper/5338",
    "Claremont": "claremont/433",
    "Claremont Upper": "claremont/claremont-upper/5611",
    "Newlands": "newlands/763",
    "Rondebosch": "rondebosch/435",
    "Rondebosch East": "rondebosch/rondebosch-east/838",
    "Mowbray": "mowbray/1506",
    "Rosebank": "rosebank/1910",
    "Observatory": "observatory/1098",
    "Pinelands": "pinelands/800",
    "Bishopscourt": "bishopscourt/799",
    "Tokai": "tokai/802",
    "Bergvliet": "bergvliet/803",
    "Meadowridge": "meadowridge/1505",
    "Diep River": "diep-river/1742",
    "Heathfield": "heathfield/1889",
    "Plumstead": "plumstead/434",
    "Constantia": "constantia/432",
    "Lakeside": "lakeside/1799",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-ZA,en;q=0.9",
}

DETAIL_RE = re.compile(r"/for-sale/.+/T\d+$")
PRICE_RE = re.compile(r"R\s*([0-9]{1,3}(?:[\s,\u00A0][0-9]{3})+)")


def load_config() -> dict[str, Any]:
    return json.loads(Path("search-config.json").read_text())


def build_search_url(code_path: str, budget_min: int, budget_max: int, beds: int) -> str:
    path = LISTING_PATH_TEMPLATE.format(slug="", code="").replace("//", "/")
    base = urljoin(BASE, f"/for-sale/western-cape/cape-town/southern-suburbs/{code_path}")
    return f"{base}?fp={budget_min}&tp={budget_max}&mnb={beds}"


def fetch(session: requests.Session, url: str, retries: int = 2) -> requests.Response | None:
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=25)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 503):
                time.sleep(4 + attempt * 3)
                continue
            return resp
        except requests.RequestException:
            time.sleep(3 + attempt * 2)
    return None


def extract_listing_links(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if DETAIL_RE.search(href):
            links.add(urljoin(BASE, href.split("?")[0]))
    return sorted(links)


def parse_int(text: str) -> int | None:
    match = re.search(r"([0-9]+)", text.replace(",", ""))
    return int(match.group(1)) if match else None


def parse_detail(session: requests.Session, url: str) -> dict[str, Any] | None:
    resp = fetch(session, url)
    if not resp or resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "lxml")
    text = soup.get_text(" ", strip=True)

    price = 0
    m = PRICE_RE.search(text)
    if m:
        digits = re.sub(r"[^\d]", "", m.group(1))
        if digits:
            price = int(digits)

    address = ""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        stack = [data] if not isinstance(data, list) else list(data)
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if "streetAddress" in item and isinstance(item["streetAddress"], str):
                    address = item["streetAddress"].strip()
                    break
                stack.extend(item.values())
        if address:
            break

    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else ""

    def label_value(label: str) -> int | None:
        pattern = re.compile(rf"([0-9]+)\s*{label}", re.IGNORECASE)
        m2 = pattern.search(text)
        return int(m2.group(1)) if m2 else None

    beds = label_value("bed") or 0
    baths = label_value("bath") or 0
    garages = label_value("garage") or 0

    return {
        "url": url,
        "title": title,
        "address": address or title,
        "price": price,
        "bedrooms": beds,
        "bathrooms": baths,
        "garage": garages,
    }


def canonical_url(url: str) -> str:
    return url.split("?")[0].rstrip("/").lower()


def load_existing_urls(csv_path: Path) -> set[str]:
    urls: set[str] = set()
    if not csv_path.exists():
        return urls
    with csv_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            u = row.get("url")
            if u:
                urls.add(canonical_url(u))
    return urls


def next_id(csv_path: Path) -> int:
    if not csv_path.exists():
        return 1
    max_id = 0
    with csv_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                max_id = max(max_id, int(row.get("id", "0") or 0))
            except ValueError:
                continue
    return max_id + 1


CSV_COLUMNS = [
    "id", "url", "address", "area", "price", "bedrooms", "bathrooms",
    "garage", "flatlet", "garden", "agent_name", "agent_phone", "agent_email",
    "listing_status", "status", "notes", "date_added", "score", "last_checked",
    "score_reason",
]


def append_new_rows(csv_path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    existing_rows: list[dict[str, Any]] = []
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as fh:
            existing_rows = list(csv.DictReader(fh))
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in existing_rows + rows:
            for column in CSV_COLUMNS:
                row.setdefault(column, "")
            writer.writerow(row)


def run() -> dict[str, Any]:
    config = load_config()
    budget_min = int(config["budget"]["min_zar"])
    budget_max = int(config["budget"]["max_zar"])
    beds_min = int(config["bedrooms_min"])

    csv_path = Path("listings.csv")
    existing = load_existing_urls(csv_path)
    session = requests.Session()

    today = time.strftime("%Y-%m-%d")
    new_rows: list[dict[str, Any]] = []
    outcome: dict[str, Any] = {
        "source": "www.privateproperty.co.za",
        "outcome": "SUCCESS",
        "new_listings": 0,
        "pages_searched": 0,
        "error": "",
    }
    per_region: dict[str, int] = {}

    listing_urls: list[str] = []
    seen_regions: set[str] = set()
    pages_ok = 0
    pages_fail = 0
    for region, code_path in REGION_CODES.items():
        search_url = build_search_url(code_path, budget_min, budget_max, beds_min)
        resp = fetch(session, search_url)
        if not resp or resp.status_code != 200:
            pages_fail += 1
            continue
        pages_ok += 1
        seen_regions.add(region)
        found = extract_listing_links(resp.text)
        per_region[region] = len(found)
        for link in found:
            if canonical_url(link) not in existing:
                listing_urls.append(link)
                existing.add(canonical_url(link))

    outcome["pages_searched"] = pages_ok + pages_fail
    if pages_ok == 0:
        outcome["outcome"] = "FAILED"
        outcome["error"] = "no region page returned 200"
        return {"outcome": outcome, "new_rows": [], "per_region": per_region}
    if pages_fail:
        outcome["outcome"] = "PARTIAL"
        outcome["error"] = f"{pages_fail} region pages failed"

    listing_urls = sorted(set(listing_urls))

    next_row_id = next_id(csv_path)
    for url in listing_urls:
        detail = parse_detail(session, url)
        if not detail:
            continue
        price = detail["price"]
        beds = detail["bedrooms"]
        if price and (price < budget_min or price > budget_max):
            continue
        if beds and beds < beds_min:
            continue
        new_rows.append({
            "id": str(next_row_id),
            "url": url,
            "address": detail["address"],
            "area": "",
            "price": str(price),
            "bedrooms": str(beds) if beds else "unknown",
            "bathrooms": str(detail["bathrooms"]) if detail["bathrooms"] else "",
            "garage": str(detail["garage"]) if detail["garage"] else "unknown",
            "flatlet": "unknown",
            "garden": "unknown",
            "agent_name": "",
            "agent_phone": "",
            "agent_email": "",
            "listing_status": "active",
            "status": "unseen",
            "notes": "Source: www.privateproperty.co.za (regions scraper)",
            "date_added": today,
            "score": "",
            "last_checked": today,
            "score_reason": "",
        })
        next_row_id += 1
        time.sleep(0.4)

    append_new_rows(csv_path, new_rows)
    outcome["new_listings"] = len(new_rows)
    return {"outcome": outcome, "per_region": per_region, "new_count": len(new_rows)}


if __name__ == "__main__":
    result = run()
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
