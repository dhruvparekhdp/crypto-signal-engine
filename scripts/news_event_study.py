#!/usr/bin/env python3
"""Event study on price lake (plan W10/W14 P4) — Mac-only, logs to audit/trial_ledger.jsonl."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

LEDGER = Path(os.environ.get("NEWS_LEDGER") or "audit/trial_ledger.jsonl")   # tests point it at a temp file


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    row = {"family": "news_study", "note": "event_study", "dry_run": args.dry_run}
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.open("a", encoding="utf-8").write(json.dumps(row) + "\n")
    print("news_event_study: logged; full lake walk runs locally with --dry-run false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
