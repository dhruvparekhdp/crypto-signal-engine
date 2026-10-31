#!/usr/bin/env python3
"""
GDELT + calendar history backfill (plan W14 P4) — run on the developer Mac, never on the server.

Owner Oct 2026: heavy GDELT parsing stays off the engine process (W13 budget).
"""
from __future__ import annotations

import argparse
import json
from datetime import date
import os
from pathlib import Path

LEDGER = Path(os.environ.get("NEWS_LEDGER") or "audit/trial_ledger.jsonl")   # tests point it at a temp file


def log_run(family: str, note: str, **extra) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    row = {"family": family, "note": note, **extra}
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def main() -> int:
    p = argparse.ArgumentParser(description="News history backfill (local Mac only)")
    p.add_argument("--from", dest="from_date", default="2021-01-01")
    p.add_argument("--to", dest="to_date", default=str(date.today()))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    log_run("news_study", "backfill_start", from_date=args.from_date, to_date=args.to_date, dry_run=args.dry_run)
    if args.dry_run:
        print(f"Would backfill {args.from_date} .. {args.to_date} (no network in CI)")
        return 0
    print("Backfill requires local GDELT files and price lake; run with --dry-run in CI.")
    log_run("news_study", "backfill_skipped_no_local_data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
