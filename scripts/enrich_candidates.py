#!/usr/bin/env python3
"""Enrich harvested candidate URLs and append genuinely new listings.

Reads ``search-scan-latest.json`` (produced by ``search_sources.py``),
fetches each candidate URL, tries to extract:

  - a probable address / title
  - price (ZAR)
  - bedrooms

Then filters against ``search-config.json`` (budget + bedrooms_min) and
appends validated candidates to ``listings.csv`` with ``status=unseen``
and ``listing_status=active``.

Deduplication:
  - exact URL match against existing rows
  - same-suburb fuzzy address match (case-insensitive; ignoring house
    number prefix), in which case the existing row's URL may be updated
    to the newly discovered URL.

Robust to Cloudflare / anti-bot walls: URLs that fail to fetch are
recorded but skipped.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "listings.csv"
CFG_PATH = ROOT / "search-config.json"
SCAN_PATH = ROOT / "search-scan-latest.json"
LOG_PATH = ROOT / "search-scan.log"
TODAY = dt.date.today().isoformat()

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 15


@dataclass
class Detail:
    price: Optional[int]
    beds: Optional[int]
    address: str
    suburb: str
    agent: str


def _get(url: str) -> tuple[Optional[int], Optional[str]]:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-ZA,en;q=0.9",
        },
    )
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(500_000).decode("utf-8", errors="ignore")
    except HTTPError as e:
        return e.code, None
    except (URLError, TimeoutError):
        return None, None
    except Exception:  # noqa: BLE001
        return None, None


def _extract_price(html: str) -> Optional[int]:
    """Find first plausible ZAR price on the page (R 3 500 000)."""
    for m in re.finditer(r"R\s?([\d\s,]{6,15})", html):
        digits = re.sub(r"[^\d]", "", m.group(1))
        if not digits:
            continue
        n = int(digits)
        if 1_000_000 <= n <= 50_000_000:
            return n
    return None


def _extract_beds(html: str) -> Optional[int]:
    low = html.lower()
    for pat in (
        r"(\d+)\s*bed(?:room)?s?",
        r"bedrooms?\D{0,20}(\d+)",
    ):
        m = re.search(pat, low)
        if m:
            try:
                n = int(m.group(1))
                if 1 <= n <= 10:
                    return n
            except ValueError:
                continue
    return None


def _extract_meta(html: str) -> tuple[str, str, str]:
    """Return (title, address_guess, agent_guess)."""
    title_m = re.search(r"<title>(.*?)</title>", html, re.DOTALL | re.I)
    title = (title_m.group(1) if title_m else "").strip()
    title = re.sub(r"\s+", " ", title)
    return title, "", ""


def _normalise_addr(a: str) -> str:
    a = a.lower().strip()
    a = re.sub(r"^\d+[a-z]?\s+", "", a)  # drop leading house number
    a = re.sub(r"[^a-z0-9\s]", "", a)
    return re.sub(r"\s+", " ", a).strip()


_SUBURB_STOPWORDS = {
    "for-sale", "residential", "house", "houses", "apartment", "apartments",
    "flat", "flats", "townhouse", "townhouses", "western-cape", "cape-town",
    "southern-suburbs", "results", "property", "property-details",
    "property-for-sale", "for", "sale", "en-za", "search",
}


def _suburb_for(url: str) -> str:
    """Best-effort suburb extraction from a listing URL path.

    Chooses the last (most specific) alphabetic path token that isn't a
    generic template keyword. Falls back to the empty string if none is
    plausible.
    """
    parsed = urllib.parse.urlparse(url)
    tokens = [t for t in parsed.path.lower().split("/") if t]
    candidates: list[str] = []
    for t in tokens:
        if t in _SUBURB_STOPWORDS:
            continue
        if re.fullmatch(r"[a-z][a-z-]*", t) and 3 < len(t) < 30:
            candidates.append(t)
    if not candidates:
        return ""
    return candidates[-1].replace("-", " ")


def _fetch_detail(url: str) -> Optional[Detail]:
    code, body = _get(url)
    if code != 200 or not body:
        return None
    title, addr, agent = _extract_meta(body)
    return Detail(
        price=_extract_price(body),
        beds=_extract_beds(body),
        address=title[:120],
        suburb=_suburb_for(url),
        agent=agent,
    )


def _load_rows() -> tuple[list[dict], list[str]]:
    with CSV_PATH.open() as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    for extra in ("last_checked", "score_reason"):
        if extra not in fieldnames:
            fieldnames.append(extra)
    return rows, fieldnames


def _next_id(rows: list[dict]) -> int:
    ids = [int(r["id"]) for r in rows if (r.get("id") or "").isdigit()]
    return (max(ids) + 1) if ids else 1


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text())
    min_p = int(cfg["budget"]["min_zar"])
    max_p = int(cfg["budget"]["max_zar"])
    min_bed = int(cfg["bedrooms_min"])
    regions_set = {r.lower() for r in cfg["regions"]}

    scan = json.loads(SCAN_PATH.read_text())
    candidates: list[tuple[str, str]] = scan.get("new_candidate_urls", [])

    rows, fieldnames = _load_rows()
    existing_urls = {(r.get("url") or "").split("?")[0].rstrip("/") for r in rows}
    existing_addr_keys = {
        (r.get("area", "").lower(), _normalise_addr(r.get("address", "")))
        for r in rows if r.get("address") and r["address"] != "unknown"
    }

    added: list[dict] = []
    url_updates: list[dict] = []
    skipped_dup = 0
    skipped_criteria = 0
    fetch_fail = 0
    log: list[str] = [f"# enrich_candidates run {TODAY}"]

    # Cap total wall time
    hard_deadline = time.time() + 300  # 5 minutes budget

    for source, url in candidates:
        if time.time() > hard_deadline:
            log.append("hit-hard-deadline: stopping enrichment")
            break
        clean = url.split("?")[0].rstrip("/")
        if clean in existing_urls:
            skipped_dup += 1
            continue

        detail = _fetch_detail(url)
        time.sleep(0.35)
        if detail is None:
            fetch_fail += 1
            log.append(f"FETCH-FAIL {url}")
            continue

        # Filter by criteria
        price_ok = detail.price is not None and min_p <= detail.price <= max_p
        beds_ok = detail.beds is not None and detail.beds >= min_bed
        # We accept if we have both signals matching. If price/beds are None,
        # we skip conservatively to avoid junk in the CSV.
        if not (price_ok and beds_ok):
            skipped_criteria += 1
            log.append(f"SKIP-CRITERIA {url} price={detail.price} beds={detail.beds}")
            continue

        # Deduplicate by (suburb, normalised-address)
        suburb_norm = detail.suburb.strip().lower()
        addr_key = (suburb_norm, _normalise_addr(detail.address))
        if addr_key in existing_addr_keys and addr_key[1]:
            # Update existing URL if it changed
            for r in rows:
                if (r.get("area", "").lower() == suburb_norm and
                        _normalise_addr(r.get("address", "")) == addr_key[1] and
                        (r.get("url") or "").split("?")[0].rstrip("/") != clean):
                    r["url"] = clean
                    url_updates.append({"id": r["id"], "new_url": clean})
                    log.append(f"URL-UPDATE #{r['id']} -> {clean}")
                    break
            skipped_dup += 1
            continue

        row_id = _next_id(rows)
        area_guess = detail.suburb.title() if detail.suburb else "unknown"
        # Only accept region-matching (be careful about "cape-town" tokens)
        if area_guess.lower() not in regions_set:
            skipped_criteria += 1
            log.append(f"SKIP-REGION {url} area_guess={area_guess}")
            continue

        new_row = {k: "" for k in fieldnames}
        new_row.update({
            "id": str(row_id),
            "url": clean,
            "address": detail.address or "unknown",
            "area": area_guess,
            "price": str(detail.price),
            "bedrooms": str(detail.beds),
            "bathrooms": "unknown",
            "garage": "unknown",
            "flatlet": "unknown",
            "garden": "unknown",
            "agent_name": "unknown",
            "agent_phone": "",
            "agent_email": "",
            "listing_status": "active",
            "status": "unseen",
            "notes": f"Auto-discovered from {source} on {TODAY}",
            "date_added": TODAY,
            "score": "",
            "last_checked": TODAY,
            "score_reason": "",
        })
        rows.append(new_row)
        added.append(new_row)
        existing_urls.add(clean)
        existing_addr_keys.add(addr_key)
        log.append(f"ADDED #{row_id} {area_guess} R{detail.price} {detail.beds}bed {url}")

    with CSV_PATH.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})

    with LOG_PATH.open("a") as fh:
        fh.write("\n".join(log) + "\n")

    summary = {
        "candidates_seen": len(candidates),
        "added": len(added),
        "url_updates": len(url_updates),
        "skipped_dup": skipped_dup,
        "skipped_criteria": skipped_criteria,
        "fetch_fail": fetch_fail,
    }
    print(json.dumps(summary, indent=2))
    (ROOT / "enrich-latest.json").write_text(json.dumps({
        "summary": summary,
        "added_ids": [r["id"] for r in added],
        "url_updates": url_updates,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
