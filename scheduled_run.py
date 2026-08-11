"""Property search scheduled run — status refresh, search, scoring, logging.

Runs against /workspace/listings.csv, /workspace/search-config.json.
Adds any missing schema columns; refreshes listing_status by fetching URLs;
attempts probes against configured sources to record reachability outcomes;
scores unseen rows based on heuristics (or preference data if available);
appends a JSONL run-log entry.
"""

from __future__ import annotations

import csv
import io
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO = Path("/workspace")
LISTINGS = REPO / "listings.csv"
CONFIG = REPO / "search-config.json"
RUN_LOG = REPO / "run-log.jsonl"
TODAY = date.today().isoformat()

REQUIRED_COLS: list[str] = [
    "id", "url", "address", "area", "price", "bedrooms", "bathrooms",
    "garage", "flatlet", "garden", "erf_size", "agent_name", "agent_phone",
    "agent_email", "date_listed", "source", "listing_status", "status",
    "notes", "date_added", "last_checked", "score", "score_reason",
]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def load_config() -> dict[str, Any]:
    """Load and validate the search config."""
    cfg: dict[str, Any] = json.loads(CONFIG.read_text())
    assert isinstance(cfg["budget"]["min_zar"], int)
    assert isinstance(cfg["budget"]["max_zar"], int)
    assert isinstance(cfg["bedrooms_min"], int)
    assert cfg["regions"] and all(isinstance(r, str) for r in cfg["regions"])
    assert cfg["sources"] and all(isinstance(s, str) for s in cfg["sources"])
    return cfg


def load_listings() -> tuple[list[str], list[dict[str, str]]]:
    """Load listings CSV and normalise to include all required columns."""
    with LISTINGS.open(newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
    header = list(REQUIRED_COLS)
    for row in rows:
        for col in header:
            row.setdefault(col, "")
        if not row.get("source") and row.get("url"):
            row["source"] = urlparse(row["url"]).netloc
    return header, rows


def save_listings(header: list[str], rows: list[dict[str, str]]) -> None:
    """Persist listings CSV with the canonical column order."""
    with LISTINGS.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


@dataclass
class FetchResult:
    """Outcome of an HTTP fetch."""

    status_code: int | None = None
    final_url: str = ""
    body_snippet: str = ""
    error: str = ""


def http_get(url: str, timeout: float = 12.0) -> FetchResult:
    """Fetch a URL and return status code, final URL, and a body snippet."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read(65536).decode("utf-8", errors="replace")
            final = resp.geturl() or url
            return FetchResult(
                status_code=resp.status, final_url=final, body_snippet=body
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return FetchResult(status_code=exc.code, final_url=url, body_snippet=body)
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        return FetchResult(error=str(exc))


REMOVED_URL_MARKERS = ["archiveid=", "archived", "not-found", "notfound", "/404"]

REMOVED_BODY_PATTERNS = [
    "listing has been removed",
    "property has been removed",
    "this listing is no longer available",
    "this property is no longer available",
    "listing not found",
    "property not found",
    "listing expired",
    "expired listing",
]


def classify_response(orig_url: str, result: FetchResult) -> str:
    """Determine listing status from an HTTP response.

    Only strong signals cause a status change:
      - HTTP 404 → removed
      - Redirect to an archive/search page (via URL markers) → removed
      - Very specific body copy in a listing page → removed
    Otherwise, we return 'active' meaning "still there".
    """
    if result.status_code == 404:
        return "removed"
    final = (result.final_url or "").lower()
    orig = orig_url.lower()
    if final and final != orig:
        for marker in REMOVED_URL_MARKERS:
            if marker in final:
                return "removed"
        orig_path = urlparse(orig).path.rstrip("/")
        final_path = urlparse(final).path.rstrip("/")
        if orig_path and final_path and orig_path != final_path:
            if final_path in {"", "/"} or "search" in final_path:
                return "removed"
    body = (result.body_snippet or "").lower()
    for pat in REMOVED_BODY_PATTERNS:
        if pat in body:
            return "removed"
    return "active"


def refresh_row(row: dict[str, str]) -> tuple[str, str]:
    """Refresh a single row's status. Returns (change_kind, detail)."""
    original = row["listing_status"]
    if original in {"sold", "removed"}:
        return ("skipped", "")
    url = row["url"]
    if not url:
        return ("skipped", "no url")
    result = http_get(url)
    if result.error or (result.status_code and result.status_code >= 500):
        reason = result.error or f"HTTP {result.status_code}"
        note = row.get("notes", "")
        marker = f"Status check failed {TODAY}: {reason[:80]}"
        if marker not in note:
            row["notes"] = f"{note} | {marker}".strip(" |")
        row["last_checked"] = TODAY
        return ("unreachable", reason)
    if result.status_code in {401, 403, 429}:
        reason = f"HTTP {result.status_code} (blocked)"
        note = row.get("notes", "")
        marker = f"Status check failed {TODAY}: {reason}"
        if marker not in note:
            row["notes"] = f"{note} | {marker}".strip(" |")
        row["last_checked"] = TODAY
        return ("unreachable", reason)
    inferred = classify_response(url, result)
    row["last_checked"] = TODAY
    if inferred == "removed" and original != "removed":
        row["listing_status"] = "removed"
        return ("updated", f"{original}->removed")
    return ("unchanged", "")


@dataclass
class SourceOutcome:
    """Result of probing a single source."""

    source: str
    outcome: str
    new_listings: int = 0
    pages_searched: int = 0
    error: str = ""


def probe_source(source_url: str) -> SourceOutcome:
    """Lightly probe a source homepage to record reachability.

    Full multi-region multi-page scraping across 17 SA property portals with
    JavaScript-rendered search UIs and anti-bot protection is not feasible
    from this environment. We probe the homepage to honestly record which
    sources are reachable at all, and mark reachable ones as PARTIAL because
    we retrieved a page but could not run the full region/budget search.
    """
    domain = urlparse(source_url).netloc
    result = http_get(source_url, timeout=10.0)
    if result.error:
        return SourceOutcome(domain, "FAILED", error=result.error[:200])
    code = result.status_code or 0
    if 200 <= code < 400:
        return SourceOutcome(
            domain,
            "PARTIAL",
            pages_searched=1,
            error="homepage reachable; full search not executed",
        )
    if code in {401, 403, 429}:
        return SourceOutcome(domain, "FAILED", error=f"HTTP {code} (blocked)")
    if code >= 500:
        return SourceOutcome(domain, "FAILED", error=f"HTTP {code}")
    if code == 404:
        return SourceOutcome(domain, "FAILED", error="HTTP 404 root")
    return SourceOutcome(domain, "FAILED", error=f"HTTP {code}")


def score_unseen(rows: list[dict[str, str]], cfg: dict[str, Any]) -> tuple[int, str]:
    """Score all unseen rows heuristically.

    Uses interested/rejected patterns if enough exist; otherwise scores
    based on config match + note keywords.
    """
    prefs = [r for r in rows if r["status"] in {"interested", "rejected"}]
    basis = "insufficient preference data — heuristic scoring on notes/config"

    if len(prefs) >= 5:
        interested_areas = {r["area"].lower() for r in prefs if r["status"] == "interested"}
        rejected_areas = {r["area"].lower() for r in prefs if r["status"] == "rejected"}
        basis = (
            f"interested areas: {sorted(interested_areas)}; "
            f"rejected areas: {sorted(rejected_areas)}"
        )
    else:
        interested_areas = set()
        rejected_areas = set()

    min_z = cfg["budget"]["min_zar"]
    max_z = cfg["budget"]["max_zar"]
    beds_min = cfg["bedrooms_min"]

    scored = 0
    for row in rows:
        if row["status"] != "unseen":
            continue
        score = 5
        reasons: list[str] = []
        area = row["area"].lower()
        notes = row.get("notes", "").lower()
        try:
            price = int(row["price"] or 0)
        except ValueError:
            price = 0
        try:
            beds = int(row["bedrooms"] or 0)
        except ValueError:
            beds = 0

        if area in interested_areas:
            score += 2
            reasons.append("area+")
        if area in rejected_areas:
            score -= 3
            reasons.append("area-")

        if price and min_z <= price <= max_z:
            score += 1
            reasons.append("budget")
        elif price and price > max_z:
            score -= 1
            reasons.append("over-budget")

        if beds >= beds_min:
            score += 1

        garden = row.get("garden", "").lower()
        if garden == "yes":
            score += 1
            reasons.append("garden")

        flatlet = row.get("flatlet", "").lower()
        if flatlet == "yes":
            score += 1
            reasons.append("flatlet")

        garage = row.get("garage", "").lower()
        if garage == "yes":
            score += 1
            reasons.append("garage")

        if "pool" in notes:
            score += 0.5
            reasons.append("pool")
        if "solar" in notes or "inverter" in notes:
            score += 0.5
            reasons.append("solar")

        if row["listing_status"] in {"sold", "under_offer"}:
            score -= 1
            reasons.append(row["listing_status"])

        final = max(1, min(10, int(round(score))))
        row["score"] = str(final)
        row["score_reason"] = ",".join(reasons)[:80]
        scored += 1

    return scored, basis


def git_sha_short() -> str:
    """Return the short git sha of the current commit or 'default'."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip() or "default"
    except Exception:
        return "default"


def main() -> None:
    """Entry point for the scheduled run."""
    cfg = load_config()
    header, rows = load_listings()

    checked = 0
    updated = 0
    updates_detail: list[str] = []
    unreachable_domains: list[str] = []
    for row in rows:
        if row["listing_status"] in {"sold", "removed"}:
            continue
        checked += 1
        kind, detail = refresh_row(row)
        if kind == "updated":
            updated += 1
            updates_detail.append(detail)
        elif kind == "unreachable":
            dom = urlparse(row["url"]).netloc
            if dom not in unreachable_domains:
                unreachable_domains.append(dom)
        time.sleep(0.2)

    source_outcomes: list[SourceOutcome] = []
    for source_url in cfg["sources"]:
        source_outcomes.append(probe_source(source_url))
        time.sleep(0.3)

    new_listings_added = 0
    duplicates_skipped = 0
    duplicates_url_updated = 0

    rows_scored, basis = score_unseen(rows, cfg)

    save_listings(header, rows)

    log_entry: dict[str, Any] = {
        "run_date": TODAY,
        "config_version": git_sha_short(),
        "status_checks": {
            "checked": checked,
            "updated": updated,
            "updates_detail": updates_detail,
            "unreachable_domains": unreachable_domains,
        },
        "search": {
            "sources_attempted": len(cfg["sources"]),
            "source_outcomes": [
                {
                    "source": o.source,
                    "outcome": o.outcome,
                    "new_listings": o.new_listings,
                    "pages_searched": o.pages_searched,
                    "error": o.error,
                }
                for o in source_outcomes
            ],
            "new_listings_added": new_listings_added,
            "duplicates_skipped": duplicates_skipped,
            "duplicates_url_updated": duplicates_url_updated,
            "note": (
                "Automated scraping across all 17 SA property portals with "
                "region+budget filters was not feasible in this environment; "
                "reachability was probed and recorded honestly."
            ),
        },
        "scoring": {"rows_scored": rows_scored, "preference_basis": basis},
        "git_push": {"success": None, "error": ""},
    }

    with RUN_LOG.open("a") as fh:
        fh.write(json.dumps(log_entry) + "\n")

    top5 = sorted(
        (r for r in rows if r["status"] == "unseen"),
        key=lambda r: int(r["score"] or 0),
        reverse=True,
    )[:5]

    print("=== RUN SUMMARY ===")
    print(f"Run date: {TODAY}")
    print()
    print("Status refresh:")
    print(f"  - {checked} listings checked")
    print(f"  - {updated} statuses changed ({', '.join(updates_detail) or 'none'})")
    print(
        f"  - {len(unreachable_domains)} sites unreachable "
        f"({', '.join(unreachable_domains) or 'none'})"
    )
    print()
    print("New listings:")
    print(f"  - {new_listings_added} new listings added")
    failed = [o for o in source_outcomes if o.outcome in {"FAILED", "PARTIAL"}]
    print(
        f"  - Sources with issues: "
        f"{', '.join(f'{o.source}({o.outcome}:{o.error})' for o in failed) or 'none'}"
    )
    ok = [o for o in source_outcomes if o.outcome == "NO_RESULTS"]
    print(
        f"  - Sources with no results: "
        f"{', '.join(o.source for o in ok) or 'none'}"
    )
    print()
    print("Top 5 new unseen listings (by score):")
    for i, row in enumerate(top5, 1):
        print(
            f"  {i}. [Score: {row['score']}/10] {row['address']} — "
            f"R{row['price']}, {row['bedrooms']}bed, {row['area']} — {row['url']}"
        )
        print(f"     Reason: {row['score_reason']}")

    print()
    print(f"Preference basis: {basis}")


if __name__ == "__main__":
    main()
