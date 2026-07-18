#!/usr/bin/env python3
"""Assemble the daily run summary and append it to run-log.jsonl.

Consumes intermediate JSON outputs (status_summary.json, search_summary.json)
and emits both a JSONL entry and a plain-text human report.
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent
CSV_PATH = BASE / "listings.csv"
STATUS_SUMMARY = BASE / "status_summary.json"
SEARCH_SUMMARY = BASE / "search_summary.json"
RUN_LOG = BASE / "run-log.jsonl"
REPORT_PATH = BASE / "run-report.txt"
CONFIG_PATH = BASE / "search-config.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def config_version() -> str:
    try:
        sha = subprocess.check_output(
            ["git", "log", "-n", "1", "--format=%h", "--", "search-config.json"],
            cwd=BASE, text=True,
        ).strip()
        return sha or "default"
    except subprocess.CalledProcessError:
        return "default"


def to_int(v: str | None) -> int:
    try:
        return int((v or "").strip())
    except (TypeError, ValueError):
        return 0


def load_rows() -> list[dict]:
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def new_today_rows(rows: list[dict], today: str) -> list[dict]:
    return [r for r in rows if (r.get("date_added") or "").strip() == today]


def top_new_by_score(rows: list[dict], n: int = 5) -> list[dict]:
    def score(r): return to_int(r.get("score"))
    unseen = [r for r in rows if (r.get("status") or "").lower() == "unseen"]
    return sorted(unseen, key=score, reverse=True)[:n]


def format_report(payload: dict, top: list[dict]) -> str:
    lines: list[str] = []
    lines.append(f"Run date: {payload['run_date']}")
    lines.append("")
    lines.append("Status refresh:")
    sc = payload["status_checks"]
    lines.append(f"  - {sc['checked']} listings checked")
    updates = sc.get("updates_detail", [])
    detail = ", ".join(updates) if updates else "0 statuses changed"
    lines.append(f"  - {sc['updated']} statuses changed ({detail})")
    lines.append(
        f"  - {len(sc['unreachable_domains'])} sites unreachable during status "
        f"check: {', '.join(sc['unreachable_domains']) or 'none'}"
    )
    lines.append("")
    lines.append("New listings:")
    lines.append(f"  - {payload['search']['new_listings_added']} new listings added")
    failed = [o for o in payload["search"]["source_outcomes"]
              if o["outcome"] in {"FAILED", "PARTIAL"}]
    no_res = [o["source"] for o in payload["search"]["source_outcomes"]
              if o["outcome"] == "NO_RESULTS"]
    if failed:
        lines.append("  - Sources with issues:")
        for o in failed:
            lines.append(
                f"      * {o['source']}: {o['outcome']} — {o.get('error') or 'unknown'}"
            )
    else:
        lines.append("  - Sources with issues: none")
    lines.append(f"  - Sources with no results: {', '.join(no_res) or 'none'}")
    lines.append("")
    lines.append("Top 5 new/unseen listings (by score):")
    for i, r in enumerate(top, start=1):
        price = r.get("price") or "?"
        beds = r.get("bedrooms") or "?"
        addr = (r.get("address") or "unknown").strip()
        area = r.get("area") or "?"
        url = r.get("url") or ""
        reason = r.get("score_reason") or ""
        lines.append(
            f"  {i}. [Score: {r.get('score')}/10] {addr} — R{price}, {beds}bed, "
            f"{area}"
        )
        lines.append(f"     URL: {url}")
        lines.append(f"     Reason: {reason}")
    lines.append("")
    push = payload["git_push"]
    lines.append(f"Git push: {'SUCCESS' if push['success'] else 'FAILED: ' + push['error']}")
    lines.append("")
    lines.append("⚠ Attention needed:")
    attn = payload.get("attention", [])
    if attn:
        for item in attn:
            lines.append(f"  - {item}")
    else:
        lines.append("  - none")
    return "\n".join(lines) + "\n"


def find_attention(rows: list[dict]) -> list[str]:
    attn: list[str] = []
    for r in rows:
        note = r.get("notes") or ""
        if note.count("Status check failed") >= 3:
            attn.append(f"listing id {r.get('id')} status check failed 3+ times")
    return attn


def build_payload(run_date: str, status: dict, search: dict, scoring: dict,
                  git_push: dict, attention: list[str]) -> dict:
    return {
        "run_date": run_date,
        "config_version": config_version(),
        "status_checks": status,
        "search": search,
        "scoring": scoring,
        "git_push": git_push,
        "attention": attention,
    }


def parse_status_from_log() -> dict:
    """Read status_summary.json when present, else return zeros."""
    default = {
        "checked": 0,
        "updated": 0,
        "unreachable_domains": [],
        "updates_detail": [],
    }
    return load_json(STATUS_SUMMARY, default)


def load_search_summary() -> dict:
    return load_json(SEARCH_SUMMARY, {
        "sources_attempted": 0,
        "source_outcomes": [],
        "new_listings_added": 0,
        "duplicates_skipped": 0,
        "duplicates_url_updated": 0,
    })


def main() -> None:
    run_date = date.today().isoformat()
    rows = load_rows()

    status = parse_status_from_log()
    search = load_search_summary()

    interested_or_rejected = sum(
        1 for r in rows
        if (r.get("status") or "").lower() in {"interested", "rejected"}
    )
    scoring = {
        "rows_scored": sum(1 for r in rows if (r.get("status") or "").lower() == "unseen"),
        "preference_basis": (
            "heuristic (garden/flatlet/garage/area/budget) — "
            f"{interested_or_rejected} labelled rows; insufficient preference data"
            if interested_or_rejected < 5
            else "learned from interested/rejected labels"
        ),
    }

    git_push = load_json(BASE / "git_push_result.json", {"success": False, "error": "not attempted"})

    payload = build_payload(
        run_date, status, search, scoring, git_push, find_attention(rows)
    )

    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")

    top = top_new_by_score(rows, 5)
    REPORT_PATH.write_text(format_report(payload, top))
    print(REPORT_PATH.read_text())


if __name__ == "__main__":
    main()
