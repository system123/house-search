#!/usr/bin/env python3
"""Detect and report user-touched rows in the working listings.csv.

How the cron *knows* when the user has updated a property:

1. Each scheduled run starts on a fresh branch checked out from the latest
   `main` commit. Anything the user has merged or pushed to `main` since
   the previous run is therefore already present in the working tree.
2. A row is considered "user-touched" when ANY of these signals are true:
      - `status` ∈ {interested, rejected, viewed, contacted, archived}
        (i.e. anything other than "unseen")
      - `user_updated_at` column has a value (the user wrote a timestamp)
      - The most recent git commit touching that line was NOT authored by
        this automation (i.e. by anyone other than `cursor-property-search`
        or whatever git author this automation uses).
3. The rest of the cron run treats user-touched rows as read-only for
   non-system fields (`status`, `notes`, `score`, `score_reason`,
   `address`, `bedrooms`, etc.). Only `listing_status`, `last_checked`,
   and appended status-check error notes may be modified.

This script does NOT pull or overlay any state from `origin/main` — that
would risk overwriting cron updates from the current run with stale data.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

BASE = Path(__file__).parent
CSV_PATH = BASE / "listings.csv"

USER_STATUSES = {
    "interested", "rejected", "viewed", "contacted", "archived",
    "shortlist", "shortlisted", "favourite", "favorite", "offer",
}


def is_user_touched(row: dict[str, str]) -> bool:
    """Detect explicit user signals only.

    Specifically:
      - status field is set to a user-decision value (interested, rejected,
        viewed, etc.), or
      - user_updated_at column has a non-empty value.

    Git blame is intentionally NOT used here, because the user originally
    authored the seed rows but hasn't necessarily reviewed each one.
    """
    status = (row.get("status") or "").strip().lower()
    if status and status != "unseen" and status in USER_STATUSES:
        return True
    if (row.get("user_updated_at") or "").strip():
        return True
    return False


def main() -> int:
    with CSV_PATH.open() as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])

    if "user_updated_at" not in fieldnames:
        fieldnames.append("user_updated_at")
        for r in rows:
            r.setdefault("user_updated_at", "")

    touched_ids: list[str] = []
    for r in rows:
        if is_user_touched(r):
            touched_ids.append(r["id"])

    with CSV_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(json.dumps({
        "total_rows": len(rows),
        "user_touched_ids": touched_ids,
        "user_touched_count": len(touched_ids),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
