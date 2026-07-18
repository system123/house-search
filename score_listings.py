#!/usr/bin/env python3
"""Preference scoring pass.

There are no `interested` / `rejected` labelled rows yet, so scoring falls
back to heuristics inferred from the notes column (garden, flatlet, garage,
price sanity, bedrooms, listing status). Every `unseen` row gets a fresh
score (1..10) plus a compact score_reason.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

BASE = Path(__file__).resolve().parent
CSV_PATH = BASE / "listings.csv"
CONFIG_PATH = BASE / "search-config.json"

PREFERRED_AREAS = {
    "pinelands", "rondebosch", "claremont", "claremont upper", "newlands",
    "bergvliet", "kenilworth upper", "rondebosch east",
}
NEUTRAL_AREAS = {
    "wynberg", "wynberg upper", "kenilworth", "plumstead", "meadowridge",
    "rosebank", "mowbray", "lakeside", "tokai",
}


def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return json.load(f)


def is_yes(v: str | None) -> bool:
    return (v or "").strip().lower() == "yes"


def to_int(v: str | None) -> int:
    try:
        return int((v or "").strip())
    except (TypeError, ValueError):
        return 0


def score_row(row: dict, budget_min: int, budget_max: int,
              bedrooms_min: int) -> tuple[int, str]:
    """Return (score, one-line reason under 80 chars)."""
    reasons: list[str] = []
    score = 5

    area = (row.get("area") or "").strip().lower()
    if area in PREFERRED_AREAS:
        score += 1
        reasons.append(f"area+ ({area})")
    elif area not in NEUTRAL_AREAS and area:
        score -= 1
        reasons.append(f"area- ({area})")

    if is_yes(row.get("garden")):
        score += 1
        reasons.append("garden")
    if is_yes(row.get("flatlet")):
        score += 2
        reasons.append("flatlet")
    if is_yes(row.get("garage")):
        score += 1
        reasons.append("garage")

    beds = to_int(row.get("bedrooms"))
    if beds >= 4:
        score += 1
        reasons.append(f"{beds}bed")
    elif beds and beds < bedrooms_min:
        score -= 2
        reasons.append(f"{beds}bed<min")

    price = to_int(row.get("price"))
    if price == 0:
        reasons.append("no price")
    elif price < budget_min:
        score -= 1
        reasons.append("under min")
    elif price > budget_max:
        score -= 2
        reasons.append("over max")
    elif price <= budget_min + (budget_max - budget_min) * 0.4:
        score += 1
        reasons.append("good value")

    listing_status = (row.get("listing_status") or "").strip().lower()
    if listing_status in {"sold", "removed"}:
        score -= 4
        reasons.append(listing_status)
    elif listing_status == "under_offer":
        score -= 1
        reasons.append("under offer")

    score = max(1, min(10, score))
    reason = ", ".join(reasons)[:80]
    return score, reason


def load_rows(path: Path) -> tuple[list[dict], list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        return list(reader), fields


def main() -> None:
    config = load_config()
    budget_min: int = config["budget"]["min_zar"]
    budget_max: int = config["budget"]["max_zar"]
    bedrooms_min: int = config["bedrooms_min"]

    rows, fields = load_rows(CSV_PATH)
    for col in ("last_checked", "score_reason"):
        if col not in fields:
            fields.append(col)
            for r in rows:
                r.setdefault(col, "")

    interested_or_rejected = sum(
        1 for r in rows
        if (r.get("status") or "").strip().lower() in {"interested", "rejected"}
    )
    scored = 0
    for row in rows:
        if (row.get("status") or "").strip().lower() != "unseen":
            continue
        score, reason = score_row(row, budget_min, budget_max, bedrooms_min)
        row["score"] = str(score)
        row["score_reason"] = reason
        scored += 1

    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print(f"SCORED={scored}")
    print(f"INTERESTED_OR_REJECTED={interested_or_rejected}")
    if interested_or_rejected < 5:
        print("NOTE: insufficient preference data — heuristic scoring only")


if __name__ == "__main__":
    main()
