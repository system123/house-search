#!/usr/bin/env python3
"""Step 4: score every unseen row in listings.csv.

If there are >=5 interested/rejected rows, we'd analyse them; otherwise we use
the heuristic recorded in MEMORIES.md (insufficient preference data).
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "listings.csv"

PREFERRED = {
    "pinelands": 2, "rondebosch": 2, "claremont": 2, "claremont upper": 2,
    "kenilworth upper": 1, "kenilworth": 1, "newlands": 2, "plumstead": 1,
    "wynberg upper": 1, "rondebosch east": 1, "rosebank": 1, "bishopscourt": 1,
    "tokai": 1, "bergvliet": 1, "meadowridge": 1, "gardens": 1, "oranjezicht": 1,
    "vredehoek": 0, "observatory": 0, "mowbray": 0, "pinelands upper": 1,
}
DOWNRATED = {
    "lakeside": -1, "muizenberg": -1, "kalk bay": -1, "heathfield": -1,
    "diep river": -1, "de waterkant": -1,
}


def to_int(x: str) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def score_row(row: dict, budget: dict, bedrooms_min: int) -> tuple[int, str]:
    area_l = (row.get("area") or "").lower().strip()
    reasons: list[str] = []
    s = 5

    if area_l in PREFERRED:
        s += PREFERRED[area_l]
        if PREFERRED[area_l] > 0:
            reasons.append(f"+{PREFERRED[area_l]} {area_l}")
    elif area_l in DOWNRATED:
        s += DOWNRATED[area_l]
        reasons.append(f"{DOWNRATED[area_l]} {area_l}")

    flatlet = (row.get("flatlet") or "").lower()
    garden = (row.get("garden") or "").lower()
    garage = (row.get("garage") or "").lower()

    if flatlet == "yes":
        s += 2
        reasons.append("+2 flatlet")
    if garden == "yes":
        s += 1
        reasons.append("+1 garden")
    if garage == "yes":
        s += 1
        reasons.append("+1 garage")

    bed = to_int(row.get("bedrooms"))
    if bed >= 4:
        s += 1
        reasons.append("+1 4+bed")
    if bed and bed < bedrooms_min:
        s -= 2
        reasons.append("-2 too few beds")

    price = to_int(row.get("price"))
    if price:
        if price > 5_800_000:
            s -= 1
            reasons.append("-1 top of budget")
        if price < budget["min_zar"] or price > budget["max_zar"]:
            s -= 2
            reasons.append("-2 out of budget")

    status = (row.get("listing_status") or "").lower()
    if status == "under_offer":
        s -= 1
        reasons.append("-1 under offer")
    elif status in ("sold", "removed"):
        s -= 2
        reasons.append("-2 sold/removed")

    s = max(1, min(10, s))
    reason = ", ".join(reasons)[:80] if reasons else "neutral"
    return s, reason


def main() -> int:
    cfg = json.loads((ROOT / "search-config.json").read_text())
    budget = cfg["budget"]
    bedrooms_min = cfg["bedrooms_min"]

    rows: list[dict] = list(csv.DictReader(CSV_PATH.open()))
    interested = sum(1 for r in rows if (r.get("status") or "").lower() == "interested")
    rejected = sum(1 for r in rows if (r.get("status") or "").lower() == "rejected")
    basis = (
        f"insufficient preference data (interested={interested} rejected={rejected});"
        f" heuristic: +flatlet/+garden/+garage, prefer Pinelands/Rondebosch/Claremont,"
        f" penalize sold/under_offer/out-of-budget."
    )

    scored = 0
    for r in rows:
        if (r.get("status") or "").lower() != "unseen":
            continue
        s, reason = score_row(r, budget, bedrooms_min)
        r["score"] = str(s)
        r["score_reason"] = reason
        scored += 1

    if rows:
        with CSV_PATH.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=rows[0].keys(), extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    print(json.dumps({"rows_scored": scored, "preference_basis": basis}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
