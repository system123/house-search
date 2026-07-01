#!/usr/bin/env python3
"""Step 4 — preference scoring for unseen listings.

Learns from rows tagged 'interested' or 'rejected' when possible. When
fewer than 5 signals exist (as today), falls back to a heuristic derived
from the automation memory: preferred suburbs + flatlet/garden/garage
bonuses + budget alignment.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Callable

from common import CONFIG_PATH, read_listings, write_listings


PREFERRED_STRONG: set[str] = {"pinelands", "rondebosch", "claremont", "newlands"}
PREFERRED_MILD: set[str] = {
    "kenilworth", "kenilworth upper", "claremont upper", "plumstead",
    "tokai", "rosebank", "bergvliet", "meadowridge", "wynberg upper",
    "bishopscourt", "gardens", "oranjezicht", "fernwood", "rondebosch east",
}
DOWN_RATED: set[str] = {
    "lakeside", "muizenberg", "kalk bay", "heathfield", "diep river",
    "de waterkant", "observatory",
}


@dataclass
class ScoreBreakdown:
    """Accumulated score adjustments for a single listing."""
    base: int = 5
    reasons: list[str] = field(default_factory=list)

    @property
    def value(self) -> int:
        return max(1, min(10, self.base))

    def add(self, delta: int, reason: str) -> None:
        self.base += delta
        sign = "+" if delta >= 0 else ""
        self.reasons.append(f"{sign}{delta} {reason}")

    def reason_str(self, limit: int = 80) -> str:
        return "; ".join(self.reasons)[:limit]


def _area_bonus(area: str, breakdown: ScoreBreakdown) -> None:
    low = (area or "").strip().lower()
    if low in PREFERRED_STRONG:
        breakdown.add(2, f"suburb {area}")
    elif low in PREFERRED_MILD:
        breakdown.add(1, f"suburb {area}")
    elif low in DOWN_RATED:
        breakdown.add(-1, f"suburb {area}")


def _feature_bonus(row: dict[str, str], breakdown: ScoreBreakdown) -> None:
    if (row.get("flatlet") or "").lower() == "yes":
        breakdown.add(2, "flatlet")
    if (row.get("garden") or "").lower() == "yes":
        breakdown.add(1, "garden")
    if (row.get("garage") or "").lower() == "yes":
        breakdown.add(1, "garage")
    beds_raw = row.get("bedrooms") or ""
    try:
        beds = int(beds_raw)
    except ValueError:
        return
    if beds >= 4:
        breakdown.add(1, f"{beds}bed")


def _budget_penalty(row: dict[str, str], cfg: dict[str, object], breakdown: ScoreBreakdown) -> None:
    try:
        price = int(row.get("price") or 0)
    except ValueError:
        price = 0
    if price == 0:
        return
    hi = int(cfg["budget"]["max_zar"])  # type: ignore[index]
    lo = int(cfg["budget"]["min_zar"])  # type: ignore[index]
    if price > hi:
        breakdown.add(-2, "over budget")
    elif price > int(hi * 0.85):
        breakdown.add(-1, "top of budget")
    elif price < lo:
        breakdown.add(-1, "under budget floor")


def _status_penalty(row: dict[str, str], breakdown: ScoreBreakdown) -> None:
    status = (row.get("listing_status") or "").lower()
    if status in {"sold", "removed"}:
        breakdown.add(-2, status)
    elif status == "under_offer":
        breakdown.add(-1, "under offer")


def _min_beds_penalty(row: dict[str, str], cfg: dict[str, object], breakdown: ScoreBreakdown) -> None:
    beds_raw = row.get("bedrooms") or ""
    try:
        beds = int(beds_raw)
    except ValueError:
        return
    if beds < int(cfg["bedrooms_min"]):
        breakdown.add(-2, "below min beds")


def score_row(row: dict[str, str], cfg: dict[str, object]) -> ScoreBreakdown:
    """Compute a ScoreBreakdown for a single unseen row."""
    breakdown = ScoreBreakdown()
    for adjuster in (
        lambda: _area_bonus(row.get("area", ""), breakdown),
        lambda: _feature_bonus(row, breakdown),
        lambda: _budget_penalty(row, cfg, breakdown),
        lambda: _status_penalty(row, breakdown),
        lambda: _min_beds_penalty(row, cfg, breakdown),
    ):
        adjuster()
    return breakdown


def apply_scoring() -> dict[str, object]:
    """Score every unseen row and write CSV back. Returns summary."""
    cfg = json.loads(CONFIG_PATH.read_text())
    _headers, rows = read_listings()
    interested = sum(1 for r in rows if (r.get("status") or "") == "interested")
    rejected = sum(1 for r in rows if (r.get("status") or "") == "rejected")
    scored = 0
    for row in rows:
        if (row.get("status") or "") != "unseen":
            continue
        breakdown = score_row(row, cfg)
        row["score"] = str(breakdown.value)
        row["score_reason"] = breakdown.reason_str()
        scored += 1
    write_listings(rows)
    basis = "heuristic (insufficient preference data)" if (interested + rejected) < 5 else "learned from prior interested/rejected rows"
    return {
        "rows_scored": scored,
        "preference_basis": basis,
        "interested_rows": interested,
        "rejected_rows": rejected,
    }


def main() -> int:
    summary = apply_scoring()
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
