"""
Keep at most a year in the database; older rows go to JSON files.

The server does this every 6 hours by itself. This script is for running it
by hand, checking what it would do, or reading the files back.

    python -m scripts.cold_storage --dry-run          # rows that would move, per table
    python -m scripts.cold_storage                    # move them now
    python -m scripts.cold_storage --days 400         # a different cutoff
    python -m scripts.cold_storage --status           # what the files hold
    python -m scripts.cold_storage --read crypto_signal_log --since 2025-01-01 > old.jsonl

Files: data/archive/{table}/{YYYY-MM}.jsonl.gz (settings.cold_storage_dir).
Back this folder up with the database backups; it IS the old history.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
from datetime import datetime
from pathlib import Path

from config.settings import settings
from storage.cold_storage import TIME_COLUMN, offload, read_rows


async def main(args) -> int:
    root = Path(args.root)
    if args.status:
        for table in sorted(TIME_COLUMN):
            folder = root / table
            files = sorted(folder.glob("*.jsonl.gz")) if folder.exists() else []
            if not files:
                continue
            size = sum(f.stat().st_size for f in files) / 1e6
            rows = 0
            for f in files:
                with gzip.open(f, "rt") as fh:
                    rows += sum(1 for _ in fh)
            print(f"{table:30} {files[0].stem[:7]} .. {files[-1].stem[:7]}  "
                  f"{rows:>10,} rows  {size:8.1f} MB")
        return 0
    if args.read:
        start = datetime.fromisoformat(args.since) if args.since else None
        for r in read_rows(root, args.read, start=start):
            print(json.dumps(r))
        return 0

    from storage.database import AsyncSessionFactory, init_db
    await init_db()
    async with AsyncSessionFactory() as session:
        moved = await offload(session, root, days=args.days, dry_run=args.dry_run)
    verb = "would move" if args.dry_run else "moved"
    for table, n in moved.items():
        if n:
            print(f"{table:30} {verb} {n:,} rows")
    print(f"total {verb}: {sum(moved.values()):,} rows older than {args.days} days")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=settings.cold_storage_dir)
    ap.add_argument("--days", type=int, default=settings.db_retention_days)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--read", default="", help="Table name to print back as JSON lines")
    ap.add_argument("--since", default="")
    raise SystemExit(asyncio.run(main(ap.parse_args())))
