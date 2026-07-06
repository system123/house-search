#!/usr/bin/env python3
"""Assemble the run-log entry for this run and append to run-log.jsonl."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from common import RUN_LOG_PATH, TODAY


def build_entry(
    config_version: str,
    status: dict,
    search: dict,
    scoring: dict,
    git_push_success: bool,
    git_push_error: str = "",
) -> dict:
    unreachable_domains = sorted((status.get("unreachable_domains") or {}).keys())
    status_out = {
        "checked": status.get("checked", 0),
        "updated": status.get("updated", 0),
        "unreachable_domains": unreachable_domains,
        "transitions": status.get("transitions", {}),
    }
    return {
        "run_date": TODAY,
        "config_version": config_version,
        "status_checks": status_out,
        "search": search,
        "scoring": scoring,
        "git_push": {"success": git_push_success, "error": git_push_error},
    }


def main() -> int:
    args = sys.argv[1:]
    config_version = args[0]
    status = json.loads(Path(args[1]).read_text())
    search = json.loads(Path(args[2]).read_text())
    scoring = json.loads(Path(args[3]).read_text())
    push_success = args[4].lower() in {"true", "1", "yes"}
    push_error = args[5] if len(args) > 5 else ""
    entry = build_entry(config_version, status, search, scoring, push_success, push_error)
    with RUN_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    print(json.dumps(entry, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
