"""
Label the whole snapshot history in one pass. Run this after a restore.

Why it is separate from the scheduled job
-----------------------------------------
The job that runs every fifteen minutes looks at three days, because that is
all a routine pass can act on and because loading 120 days of rows four times
an hour to write a few hundred labels would be most of a small database's day.

Rows restored from a backup are historical by definition — every one of them
is outside that window the moment it lands — so the routine job would never
touch them, however long the engine ran. This walks the lot, in chunks.

    python scripts/backfill_labels.py
    python scripts/backfill_labels.py --days 30
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.snapshot_labeler import backfill_all  # noqa: E402
from storage.database import AsyncSessionFactory  # noqa: E402


async def main(days: int, chunk_days: int) -> int:
    print(f"Labelling the last {days} days of snapshots, {chunk_days} days at a time.")
    print("Nothing is deleted and nothing already labelled is touched.\n")

    async with AsyncSessionFactory() as session:
        run = await backfill_all(session, days=days, chunk_days=chunk_days)

    print(f"\n  scanned    {run.scanned:>9,}")
    print(f"  labelled   {run.labelled:>9,}")
    print(f"  unresolved {run.unresolved:>9,}")
    print(f"  coverage   {run.coverage:>9.1%}")
    if run.unresolved:
        print("\nUnresolved rows are ones with no snapshot near the horizon —")
        print("restart gaps, or the tail of the history. That is expected.")
    return 0 if run.scanned else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=120,
                        help="How far back to go (default: the retention window)")
    parser.add_argument("--chunk-days", type=int, default=7,
                        help="Rows per transaction, in days (default 7)")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.days, args.chunk_days)))
