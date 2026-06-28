#!/usr/bin/env python3
"""Apply heuristic preference scoring to unseen listings.

Since there are no rows with status in (interested, rejected) yet, scoring
uses patterns observed in the existing data: bedroom count, garden, flatlet,
garage, area preference, price band, and listing status.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

CSV_PATH = Path(__file__).parent / "listings.csv"


PREFERRED_AREAS = {
    "Pinelands": 2,
    "Rondebosch": 2,
    "Claremont": 1,
    "Claremont Upper": 1,
    "Kenilworth Upper": 1,
    "Newlands": 1,
    "Rondebosch East": 1,
    "Bergvliet": 1,
    "Meadowridge": 1,
    "Plumstead": 1,
    "Plumstead Upper": 1,
    "Tokai": 1,
    "Wynberg Upper": 1,
    "Rosebank": 1,
    "Bishopscourt": 1,
}
LESS_PREFERRED = {"Muizenberg": -1, "Diep River": -1, "Heathfield": -1}


def yes(v: str) -> bool:
    return (v or "").strip().lower() == "yes"


def no(v: str) -> bool:
    return (v or "").strip().lower() == "no"


def safe_int(v: str) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def score_row(row: dict[str, str]) -> tuple[int, str]:
    """Return (score 1-10, short reason)."""
    reasons: list[str] = []
    s = 5

    # Area preference
    area_bonus = PREFERRED_AREAS.get(row.get("area", ""), 0)
    area_bonus += LESS_PREFERRED.get(row.get("area", ""), 0)
    s += area_bonus
    if area_bonus > 0:
        reasons.append(f"area+{area_bonus}")
    elif area_bonus < 0:
        reasons.append(f"area{area_bonus}")

    # Flatlet (very preferred — present in many high-scored)
    notes_lower = (row.get("notes") or "").lower()
    flatlet_signal = yes(row.get("flatlet", "")) or "flatlet" in notes_lower or "cottage" in notes_lower or "granny flat" in notes_lower
    if flatlet_signal:
        s += 2
        reasons.append("flatlet")
    elif no(row.get("flatlet", "")):
        s -= 1
        reasons.append("no flatlet")

    # Garden
    if yes(row.get("garden", "")) or "garden" in notes_lower or "established garden" in notes_lower:
        s += 1
        reasons.append("garden")
    elif no(row.get("garden", "")):
        s -= 1
        reasons.append("no garden")

    # Garage
    if yes(row.get("garage", "")) or "garage" in notes_lower:
        s += 1
        reasons.append("garage")
    elif no(row.get("garage", "")):
        s -= 1
        reasons.append("no garage")

    # Bedrooms
    beds = safe_int(row.get("bedrooms", ""))
    if beds >= 4:
        s += 1
        reasons.append(f"{beds}bed")
    elif beds == 2:
        s -= 1
        reasons.append("only 2bed")

    # Price band
    price = safe_int(row.get("price", ""))
    if 0 < price < 4_500_000:
        s += 1
        reasons.append("good value")
    elif price > 6_500_000:
        s -= 1
        reasons.append("top of budget")

    # Listing status
    status = (row.get("listing_status") or "").lower()
    if status in ("sold", "removed"):
        s -= 2
        reasons.append(f"{status}")
    elif status == "under_offer":
        s -= 1
        reasons.append("under_offer")

    # Unknown address penalty (harder to verify)
    if (row.get("address") or "").lower().strip() in ("", "unknown"):
        s -= 1
        reasons.append("addr unknown")

    s = max(1, min(10, s))
    reason = ", ".join(reasons)[:80]
    return s, reason


USER_STATUSES = {
    "interested", "rejected", "viewed", "contacted", "archived",
    "shortlist", "shortlisted", "favourite", "favorite", "offer",
}


def main() -> int:
    with CSV_PATH.open() as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    if "score_reason" not in fieldnames:
        fieldnames.append("score_reason")

    scored = 0
    skipped_user_touched = 0
    for r in rows:
        r.setdefault("score_reason", "")
        status = (r.get("status") or "").strip().lower()
        # Only score "unseen" rows; never re-score user-touched ones.
        if status in USER_STATUSES:
            skipped_user_touched += 1
            continue
        if status != "unseen":
            continue
        score, reason = score_row(r)
        r["score"] = str(score)
        r["score_reason"] = reason
        scored += 1

    with CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Scored {scored} unseen rows; skipped {skipped_user_touched} user-touched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
