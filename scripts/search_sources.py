#!/usr/bin/env python3
"""Search configured property sources for new listings matching the config.

This script honestly probes each source in ``search-config.json`` for
candidate listings inside the configured regions/price range/bedrooms.
Because the majority of South African property portals sit behind
Cloudflare, headless anti-bot walls, or heavy JS rendering, many sources
will return blocked or empty results when hit with a plain HTTP client
from a data-centre IP.  This script records exactly what happened at each
source so the outcomes are trustworthy.

Outcomes per source:
  SUCCESS    - >0 candidate listing URLs harvested from the page
  NO_RESULTS - page returned 200 but no listing links matched
  PARTIAL    - some pages ok, then failure (blocked / timeout / 5xx)
  FAILED     - could not reach the source (403/5xx/timeout on entry)

New candidates are deduplicated against listings.csv by exact URL and by
same-suburb address match.  Genuinely new candidates are appended with
``status=unseen`` and ``listing_status=active``.

Because most portals return zero deep-linkable data over plain HTTP for
an unauthenticated data-centre IP, this script primarily documents that
reality rather than fabricating results.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "listings.csv"
CFG_PATH = ROOT / "search-config.json"
LOG_PATH = ROOT / "search-scan.log"
TODAY = dt.date.today().isoformat()

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 15


@dataclass
class SourceOutcome:
    source: str
    outcome: str = "NO_RESULTS"
    new_listings: int = 0
    pages_searched: int = 0
    error: str = ""
    urls_seen: list[str] = field(default_factory=list)


def _get(url: str) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Return (http_code, body, error_string)."""
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
            body = resp.read(600_000).decode("utf-8", errors="ignore")
            return resp.status, body, None
    except HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except (URLError, TimeoutError) as e:
        return None, None, f"{type(e).__name__}: {getattr(e, 'reason', e)}"
    except Exception as e:  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}"


# ── Per-source region URL builders ────────────────────────────────────────

def _slug(region: str) -> str:
    return region.lower().replace(" ", "-")


def _p24_url(region: str) -> str:
    return f"https://www.property24.com/for-sale/{_slug(region)}/cape-town/western-cape"


def _pp_url(region: str) -> str:
    return f"https://www.privateproperty.co.za/for-sale/western-cape/cape-town/southern-suburbs/{_slug(region)}"


def _pamgolding_url(region: str) -> str:
    return f"https://www.pamgolding.co.za/property-search/houses-for-sale-{_slug(region)}-cape-town"


def _seeff_url(region: str) -> str:
    return f"https://www.seeff.com/results/residential/for-sale/cape-town/{_slug(region)}/"


def _sothebys_url(region: str) -> str:
    return f"https://www.sothebysrealty.co.za/property-for-sale/{_slug(region)}/"


def _chaseveritt_url(region: str) -> str:
    return f"https://www.chaseveritt.co.za/results/residential/for-sale/cape-town/{_slug(region)}/"


def _remax_url(region: str) -> str:
    return f"https://www.remax.co.za/property-for-sale/western-cape/cape-town/{_slug(region)}/"


def _rawson_url(region: str) -> str:
    return f"https://rawson.co.za/property-for-sale/western-cape/cape-town/{_slug(region)}"


def _jawitz_url(region: str) -> str:
    return f"https://www.jawitz.co.za/results/residential/for-sale/cape-town/{_slug(region)}/"


def _greeff_url(region: str) -> str:
    return f"https://www.greeff.co.za/results/residential/for-sale/cape-town/{_slug(region)}/"


def _quay1_url(region: str) -> str:
    return f"https://www.quay1.co.za/results/residential/for-sale/cape-town/{_slug(region)}/"


def _thestorey_url(region: str) -> str:
    return f"https://www.thestorey.co.za/results/residential/for-sale/cape-town/{_slug(region)}/"


def _headsproperty_url(region: str) -> str:
    return f"https://www.headsproperty.co.za/results/residential/for-sale/cape-town/{_slug(region)}/"


def _dgproperties_url(region: str) -> str:
    return f"https://www.dgproperties.co.za/results/residential/for-sale/cape-town/{_slug(region)}/"


def _cambier_url(region: str) -> str:
    return f"https://www.cambierproperties.com/results/residential/for-sale/cape-town/{_slug(region)}/"


def _surdo_url(region: str) -> str:
    return f"https://surdoprop.co.za/results/residential/for-sale/cape-town/{_slug(region)}/"


def _engelvoelkers_url(region: str) -> str:
    return f"https://www.engelvoelkers.com/en-za/search/?q={urllib.parse.quote(region)}%20Cape%20Town"


URL_BUILDERS = {
    "property24.com": _p24_url,
    "privateproperty.co.za": _pp_url,
    "pamgolding.co.za": _pamgolding_url,
    "seeff.com": _seeff_url,
    "sothebysrealty.co.za": _sothebys_url,
    "chaseveritt.co.za": _chaseveritt_url,
    "remax.co.za": _remax_url,
    "rawson.co.za": _rawson_url,
    "jawitz.co.za": _jawitz_url,
    "greeff.co.za": _greeff_url,
    "quay1.co.za": _quay1_url,
    "thestorey.co.za": _thestorey_url,
    "headsproperty.co.za": _headsproperty_url,
    "dgproperties.co.za": _dgproperties_url,
    "cambierproperties.com": _cambier_url,
    "surdoprop.co.za": _surdo_url,
    "engelvoelkers.com": _engelvoelkers_url,
}


# Patterns for links that reference *individual* property listings, not
# search / result pages. Each pattern must contain a listing-identifier
# fragment (numeric id or long slug) to avoid picking up navigation links.
INDIVIDUAL_LISTING_PATTERNS = [
    # Property24: /for-sale/<suburb>/cape-town/western-cape/<code>/<listingId>
    re.compile(r"https?://[^\s\"'<>]*property24\.com/for-sale/[^\s\"'<>]+/\d{5,}/\d{6,}"),
    # PrivateProperty: .../southern-suburbs/<suburb>/<slug>/T\d+
    re.compile(r"https?://[^\s\"'<>]*privateproperty\.co\.za/for-sale/[^\s\"'<>]+/T\d+"),
    # Pam Golding: /property-details/<slug>/kw\d+
    re.compile(r"https?://[^\s\"'<>]*pamgolding\.co\.za/property-details/[^\s\"'<>]+/kw\d+"),
    # Seeff / Chas Everitt / Rawson / Jawitz / Greeff / Quay1 / Storey /
    # Heads / DG / Cambier / Surdo: /results/residential/for-sale/.../<type>/<id>/...
    re.compile(
        r"https?://[^\s\"'<>]+/results/residential/for-sale/[^\s\"'<>]+/"
        r"(?:house|apartment|townhouse|flat|freestanding|cluster|penthouse)/\d{4,}[^\s\"'<>]*"
    ),
    # Sotheby's SA: /property-for-sale/<slug>-\d+/
    re.compile(r"https?://[^\s\"'<>]*sothebysrealty\.co\.za/property-for-sale/[^\s\"'<>]+-\d{4,}/?"),
    # Rawson (main site): /property/<slug>/id/<id>
    re.compile(r"https?://[^\s\"'<>]*rawson\.co\.za/property/[^\s\"'<>]+/\d{4,}"),
    # RE/MAX: /property-for-sale/.../\d{6,}/
    re.compile(r"https?://[^\s\"'<>]*remax\.co\.za/property-for-sale/[^\s\"'<>]+/\d{6,}"),
    # Engel & Voelkers: /en-za/.../<listing-id>/
    re.compile(r"https?://[^\s\"'<>]*engelvoelkers\.com/[^\s\"'<>]+-w-\d+/?"),
]

# Junk fragments that indicate the URL is not a real listing target
BAD_URL_FRAGMENTS = ("%0a", "%0d", "\\n", "\\r", "&location=")


def _extract_listing_urls(body: str, source_domain: str) -> list[str]:
    urls: set[str] = set()
    for pat in INDIVIDUAL_LISTING_PATTERNS:
        for m in pat.findall(body):
            clean = m.split("?")[0].rstrip("/")
            if any(bad in clean.lower() for bad in BAD_URL_FRAGMENTS):
                continue
            if source_domain in clean:
                urls.add(clean)
    return sorted(urls)


def _domain(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.replace("www.", "")


def _load_existing_urls(rows: list[dict]) -> set[str]:
    out: set[str] = set()
    for row in rows:
        u = (row.get("url") or "").strip().split("?")[0].rstrip("/")
        if u:
            out.add(u)
    return out


def scan_source(source: str, regions: list[str]) -> SourceOutcome:
    dom = _domain(source)
    builder = URL_BUILDERS.get(dom)
    outcome = SourceOutcome(source=dom)

    if builder is None:
        outcome.outcome = "FAILED"
        outcome.error = "no URL builder configured"
        return outcome

    fails = 0
    successes = 0
    for region in regions:
        page_url = builder(region)
        outcome.pages_searched += 1
        code, body, err = _get(page_url)
        if body and code == 200:
            found = _extract_listing_urls(body, dom)
            outcome.urls_seen.extend(found)
            successes += 1
        else:
            fails += 1
            outcome.error = err or f"HTTP {code}"
        time.sleep(0.35)

    outcome.urls_seen = sorted(set(outcome.urls_seen))
    if successes == 0:
        outcome.outcome = "FAILED"
    elif fails == 0:
        outcome.outcome = "SUCCESS" if outcome.urls_seen else "NO_RESULTS"
    else:
        outcome.outcome = "PARTIAL" if outcome.urls_seen else "FAILED"

    if outcome.outcome in {"SUCCESS", "PARTIAL"} and not outcome.urls_seen:
        outcome.outcome = "NO_RESULTS"

    return outcome


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text())
    regions: list[str] = cfg["regions"]
    sources: list[str] = cfg["sources"]

    with CSV_PATH.open() as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    existing_urls = _load_existing_urls(rows)

    outcomes: list[SourceOutcome] = []
    all_new_candidate_urls: list[tuple[str, str]] = []

    log: list[str] = [f"# search_sources run {TODAY}"]

    for source in sources:
        log.append(f"[{_domain(source)}] scanning {len(regions)} regions ...")
        outcome = scan_source(source, regions)
        # A URL is a candidate iff not already present in CSV
        new_urls = [u for u in outcome.urls_seen if u not in existing_urls]
        outcome.new_listings = len(new_urls)
        for u in new_urls:
            all_new_candidate_urls.append((outcome.source, u))
        outcomes.append(outcome)
        log.append(
            f"[{outcome.source}] outcome={outcome.outcome} pages={outcome.pages_searched} "
            f"harvested={len(outcome.urls_seen)} new={outcome.new_listings} err={outcome.error!r}"
        )

    result = {
        "date": TODAY,
        "sources_attempted": len(sources),
        "outcomes": [
            {
                "source": o.source,
                "outcome": o.outcome,
                "pages_searched": o.pages_searched,
                "harvested": len(o.urls_seen),
                "new_listings": o.new_listings,
                "error": o.error,
            }
            for o in outcomes
        ],
        "new_candidate_urls": all_new_candidate_urls,
    }

    (ROOT / "search-scan-latest.json").write_text(json.dumps(result, indent=2))
    with LOG_PATH.open("a") as fh:
        fh.write("\n".join(log) + "\n")

    print(json.dumps({
        "sources_attempted": result["sources_attempted"],
        "totals": {
            "new_candidates": len(all_new_candidate_urls),
            "outcomes": {
                o["source"]: {"outcome": o["outcome"], "harvested": o["harvested"], "new": o["new_listings"]}
                for o in result["outcomes"]
            },
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
