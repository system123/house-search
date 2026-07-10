#!/usr/bin/env python3
"""Compose and append one run-log.jsonl entry for today's automated run.

Reads the artefact JSONs produced by the earlier steps
(``search-scan-latest.json``, ``enrich-latest.json``,
``score-latest.json``) and derives status-check counts from
``status-check.log`` for today's date.  The final object is appended as
a single line to ``run-log.jsonl``.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TODAY = dt.date.today().isoformat()


def _status_check_summary() -> dict:
    """Parse status-check.log for today's run block."""
    log_path = ROOT / "status-check.log"
    if not log_path.exists():
        return {"checked": 0, "updated": 0, "unreachable_domains": []}
    text = log_path.read_text()
    blocks = re.split(r"^# status_check run ", text, flags=re.M)
    todays = [b for b in blocks if b.startswith(TODAY)]
    if not todays:
        return {"checked": 0, "updated": 0, "unreachable_domains": []}
    latest = todays[-1]
    lines = latest.splitlines()
    updated = sum(1 for l in lines if " REMOVED " in l or " SOLD " in l or " UNDER_OFFER " in l)
    unreachable = set()
    checked = 0
    for l in lines:
        if l.startswith("#"):
            continue
        if l.strip():
            checked += 1
        m = re.search(r"UNREACHABLE .* https?://(?:www\.)?([^/]+)/", l)
        if m:
            unreachable.add(m.group(1))
    return {
        "checked": checked,
        "updated": updated,
        "unreachable_domains": sorted(unreachable),
    }


def _load(path: Path) -> dict | list:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def main() -> int:
    scan = _load(ROOT / "search-scan-latest.json") or {}
    enrich = _load(ROOT / "enrich-latest.json") or {}
    score = _load(ROOT / "score-latest.json") or {}

    entry = {
        "run_date": TODAY,
        "config_version": _git_sha(),
        "status_checks": _status_check_summary(),
        "search": {
            "sources_attempted": scan.get("sources_attempted", 0),
            "source_outcomes": [
                {
                    "source": o["source"],
                    "outcome": o["outcome"],
                    "new_listings": o["new_listings"],
                    "pages_searched": o["pages_searched"],
                    "error": o.get("error", ""),
                }
                for o in scan.get("outcomes", [])
            ],
            "new_listings_added": enrich.get("summary", {}).get("added", 0),
            "duplicates_skipped": enrich.get("summary", {}).get("skipped_dup", 0),
            "duplicates_url_updated": enrich.get("summary", {}).get("url_updates", 0),
        },
        "scoring": {
            "rows_scored": score.get("rows_scored", 0),
            "preference_basis": score.get("preference_basis", ""),
        },
        "git_push": {"success": False, "error": "pending"},
    }

    (ROOT / "run-log.jsonl").open("a").write(json.dumps(entry) + "\n")
    print(json.dumps(entry, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
