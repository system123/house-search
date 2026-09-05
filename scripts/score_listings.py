#!/usr/bin/env python3
"""Score every listings.csv row where status == 'unseen'.

When at least 5 rows have status of 'interested' or 'rejected', preferences
are inferred from those rows. Otherwise a conservative default heuristic
is used and 'insufficient preference data' is recorded in the run log.
"""
from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Optional

BASE = Path(__file__).resolve().parent.parent
CSV_PATH = BASE / "listings.csv"
CONFIG_PATH = BASE / "search-config.json"

MIN_PREF_ROWS = 5


@dataclass
class Preferences:
    """Learned preference profile. Values are in [0, 1] preferred ratios."""
    liked_areas: set[str]
    disliked_areas: set[str]
    ideal_price_min: int
    ideal_price_max: int
    prefers_garden: bool
    prefers_flatlet: bool
    prefers_garage: bool
    basis: str


def _yn(value: str) -> Optional[bool]:
    v = (value or "").strip().lower()
    if v == "yes":
        return True
    if v == "no":
        return False
    return None


def _price_int(value: str) -> Optional[int]:
    try:
        val = int(value)
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def _default_preferences(cfg: dict) -> Preferences:
    lo = cfg["budget"]["min_zar"]
    hi = cfg["budget"]["max_zar"]
    return Preferences(
        liked_areas=set(),
        disliked_areas=set(),
        ideal_price_min=lo,
        ideal_price_max=min(lo + int((hi - lo) * 0.55), hi),
        prefers_garden=True,
        prefers_flatlet=True,
        prefers_garage=True,
        basis="insufficient preference data — using neutral defaults",
    )


def _feature_ratio(rows: list[dict], column: str) -> float:
    yes = sum(1 for r in rows if _yn(r.get(column, "")) is True)
    total = sum(1 for r in rows if _yn(r.get(column, "")) is not None)
    return yes / total if total else 0.0


def _learn_preferences(rows: list[dict], cfg: dict) -> Preferences:
    interested = [r for r in rows if r.get("status") == "interested"]
    rejected = [r for r in rows if r.get("status") == "rejected"]
    if len(interested) + len(rejected) < MIN_PREF_ROWS:
        return _default_preferences(cfg)

    liked_areas = {r["area"].strip() for r in interested if r.get("area")}
    disliked_areas = {r["area"].strip() for r in rejected if r.get("area")} - liked_areas

    prices = [_price_int(r.get("price", "")) for r in interested]
    prices = [p for p in prices if p]
    if prices:
        lo = int(min(prices) * 0.9)
        hi = int(max(prices) * 1.05)
    else:
        lo, hi = cfg["budget"]["min_zar"], cfg["budget"]["max_zar"]

    basis_parts = []
    if liked_areas:
        basis_parts.append(f"prefers {sorted(liked_areas)[:5]}")
    if disliked_areas:
        basis_parts.append(f"avoids {sorted(disliked_areas)[:5]}")
    if prices:
        basis_parts.append(f"price range R{lo:,}-R{hi:,}")

    return Preferences(
        liked_areas=liked_areas,
        disliked_areas=disliked_areas,
        ideal_price_min=lo,
        ideal_price_max=hi,
        prefers_garden=_feature_ratio(interested, "garden") > _feature_ratio(rejected, "garden"),
        prefers_flatlet=_feature_ratio(interested, "flatlet") > _feature_ratio(rejected, "flatlet"),
        prefers_garage=_feature_ratio(interested, "garage") > _feature_ratio(rejected, "garage"),
        basis="; ".join(basis_parts) or "learned from interested/rejected history",
    )


def _score_row(row: dict, prefs: Preferences) -> tuple[int, str]:
    score = 5
    reasons: list[str] = []

    area = (row.get("area") or "").strip()
    if area and area in prefs.liked_areas:
        score += 2
        reasons.append("preferred area")
    elif area and area in prefs.disliked_areas:
        score -= 3
        reasons.append("avoided area")

    price = _price_int(row.get("price", ""))
    if price is not None:
        if prefs.ideal_price_min <= price <= prefs.ideal_price_max:
            score += 1
            reasons.append("in ideal price band")
        elif price > prefs.ideal_price_max:
            score -= 1
            reasons.append("above ideal price")

    if prefs.prefers_garden and _yn(row.get("garden", "")) is True:
        score += 1
        reasons.append("garden")
    if prefs.prefers_flatlet and _yn(row.get("flatlet", "")) is True:
        score += 1
        reasons.append("flatlet")
    if prefs.prefers_garage and _yn(row.get("garage", "")) is True:
        score += 1
        reasons.append("garage")

    listing_status = (row.get("listing_status") or "").strip().lower()
    if listing_status == "under_offer":
        score -= 1
        reasons.append("under offer")
    if listing_status in ("sold", "removed"):
        score = max(1, score - 3)
        reasons.append(f"listing {listing_status}")

    score = max(1, min(10, score))
    reason = ", ".join(reasons)[:80] or "neutral, no strong signals"
    return score, reason


def _load_rows() -> tuple[list[str], list[dict]]:
    with CSV_PATH.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def _save_rows(fieldnames: list[str], rows: list[dict]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            for col in fieldnames:
                row.setdefault(col, "")
            writer.writerow(row)


def main() -> int:
    cfg = json.loads(CONFIG_PATH.read_text())
    fieldnames, rows = _load_rows()
    if "score_reason" not in fieldnames:
        fieldnames.append("score_reason")
    prefs = _learn_preferences(rows, cfg)

    scored = 0
    for row in rows:
        if (row.get("status") or "").strip().lower() != "unseen":
            continue
        score, reason = _score_row(row, prefs)
        row["score"] = str(score)
        row["score_reason"] = reason
        scored += 1

    _save_rows(fieldnames, rows)
    print(json.dumps({
        "rows_scored": scored,
        "preference_basis": prefs.basis,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
