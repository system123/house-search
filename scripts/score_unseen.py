#!/usr/bin/env python3
"""Score every ``status=unseen`` row in listings.csv on a 1-10 scale.

Preference basis:
  * If there are >= 5 rows with ``status`` in {interested, rejected},
    infer feature weights from that population.
  * Otherwise, score against sensible defaults derived from
    ``search-config.json`` and general heuristics (garden + flatlet +
    garage + region + price fit). A note is written to the run log.

Every scored row gets a short (<80 char) ``score_reason``.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "listings.csv"
CFG_PATH = ROOT / "search-config.json"


@dataclass
class Prefs:
    liked_areas: set[str]
    disliked_areas: set[str]
    liked_price_max: Optional[int]
    prefers_garden: bool
    prefers_flatlet: bool
    prefers_garage: bool
    sample_size: int
    is_insufficient: bool


def _parse_int(s: str) -> Optional[int]:
    if not s:
        return None
    m = re.search(r"\d+", s.replace(",", ""))
    return int(m.group()) if m else None


def _score_yes(v: str) -> int:
    v = (v or "").strip().lower()
    if v == "yes":
        return 1
    if v == "no":
        return -1
    return 0


def infer_prefs(rows: list[dict], config: dict) -> Prefs:
    interested = [r for r in rows if (r.get("status") or "").lower() == "interested"]
    rejected = [r for r in rows if (r.get("status") or "").lower() == "rejected"]
    sample = len(interested) + len(rejected)

    if sample < 5:
        return Prefs(
            liked_areas=set(),
            disliked_areas=set(),
            liked_price_max=int(config["budget"]["max_zar"]),
            prefers_garden=True,
            prefers_flatlet=True,
            prefers_garage=True,
            sample_size=sample,
            is_insufficient=True,
        )

    liked_area_counter = Counter(r.get("area", "") for r in interested)
    disliked_area_counter = Counter(r.get("area", "") for r in rejected)
    prices = [_parse_int(r.get("price", "")) for r in interested]
    prices = [p for p in prices if p]
    liked_price_max = int(max(prices)) if prices else int(config["budget"]["max_zar"])

    return Prefs(
        liked_areas={a for a, _ in liked_area_counter.most_common(6) if a},
        disliked_areas={a for a, _ in disliked_area_counter.most_common(6) if a},
        liked_price_max=liked_price_max,
        prefers_garden=sum(_score_yes(r.get("garden", "")) for r in interested) >= 2,
        prefers_flatlet=sum(_score_yes(r.get("flatlet", "")) for r in interested) >= 2,
        prefers_garage=sum(_score_yes(r.get("garage", "")) for r in interested) >= 2,
        sample_size=sample,
        is_insufficient=False,
    )


def score_row(row: dict, prefs: Prefs, budget: dict) -> tuple[int, str]:
    """Return (score, reason) for a row given inferred preferences."""
    score = 5.0
    reasons: list[str] = []

    area = (row.get("area") or "").strip()
    if area in prefs.liked_areas:
        score += 1.5
        reasons.append(f"+area {area}")
    elif area in prefs.disliked_areas:
        score -= 2.5
        reasons.append(f"-area {area}")

    price = _parse_int(row.get("price", "") or "")
    if price:
        if budget["min_zar"] <= price <= budget["max_zar"]:
            if prefs.liked_price_max and price <= prefs.liked_price_max:
                score += 1.0
                reasons.append("+price fit")
            else:
                score += 0.3
                reasons.append("price ok")
        elif price > budget["max_zar"]:
            score -= 1.5
            reasons.append("over budget")
        elif price < budget["min_zar"]:
            score -= 0.5
            reasons.append("below range")

    if prefs.prefers_garden:
        s = _score_yes(row.get("garden", ""))
        if s > 0:
            score += 1.0
            reasons.append("+garden")
        elif s < 0:
            score -= 0.5
            reasons.append("-garden")
    if prefs.prefers_flatlet:
        s = _score_yes(row.get("flatlet", ""))
        if s > 0:
            score += 1.5
            reasons.append("+flatlet")
    if prefs.prefers_garage:
        s = _score_yes(row.get("garage", ""))
        if s > 0:
            score += 0.5
            reasons.append("+garage")
        elif s < 0:
            score -= 0.5
            reasons.append("-garage")

    # Downweight listings with known-bad status flags
    ls = (row.get("listing_status") or "").lower()
    if ls == "under_offer":
        score -= 0.8
        reasons.append("under_offer")
    elif ls in {"sold", "removed"}:
        score -= 3.0
        reasons.append(ls)

    beds = _parse_int(row.get("bedrooms", "") or "")
    if beds:
        if beds >= 3:
            score += 0.3
            reasons.append(f"{beds}bed")
        elif beds < 2:
            score -= 1.0
            reasons.append(f"only {beds}bed")

    final = max(1, min(10, round(score)))
    reason = ", ".join(reasons)[:80] if reasons else "neutral"
    return final, reason


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text())
    budget = {"min_zar": int(cfg["budget"]["min_zar"]),
              "max_zar": int(cfg["budget"]["max_zar"])}

    with CSV_PATH.open() as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    for extra in ("last_checked", "score_reason"):
        if extra not in fieldnames:
            fieldnames.append(extra)

    prefs = infer_prefs(rows, cfg)

    scored = 0
    for row in rows:
        if (row.get("status") or "").lower() != "unseen":
            continue
        score, reason = score_row(row, prefs, budget)
        row["score"] = str(score)
        row["score_reason"] = reason
        scored += 1

    with CSV_PATH.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})

    basis = (
        "insufficient preference data (<5 interested/rejected rows); "
        f"used config defaults + generic heuristics (garden/flatlet/garage, "
        f"budget {budget['min_zar']}-{budget['max_zar']})"
        if prefs.is_insufficient
        else f"inferred from {prefs.sample_size} labelled rows: liked_areas="
             f"{sorted(prefs.liked_areas)} disliked_areas={sorted(prefs.disliked_areas)}"
    )
    print(json.dumps({
        "rows_scored": scored,
        "preference_basis": basis,
        "insufficient_data": prefs.is_insufficient,
        "sample_size": prefs.sample_size,
    }, indent=2))
    (ROOT / "score-latest.json").write_text(json.dumps({
        "rows_scored": scored,
        "preference_basis": basis,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
