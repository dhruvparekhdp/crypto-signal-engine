"""
Do the signals beat entering at random? Run this after every change.

    python scripts/null_test.py
    python scripts/null_test.py --stop 0.92 --target 1.84 --hold 24
    python scripts/null_test.py --days 30 --trials 500

Reads crypto_signal_log for the entries and crypto_snapshots for the price
paths, so it works on whatever the database currently holds. Nothing is
written.

The number that matters is p. Above ~0.5 the detectors are contributing
nothing; below 0.05 there is something worth keeping, on this sample.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from analysis.instruments import spec_for  # noqa: E402
from analysis.null_test import Entry, compare_against_random, render  # noqa: E402
from storage.database import AsyncSessionFactory  # noqa: E402
from storage.models import CryptoSignalLog, CryptoSnapshot  # noqa: E402


async def load(days: int):
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    async with AsyncSessionFactory() as session:
        snaps = (await session.execute(
            select(CryptoSnapshot)
            .where(CryptoSnapshot.timestamp >= cutoff, CryptoSnapshot.price > 0)
            .order_by(CryptoSnapshot.timestamp))).scalars().all()
        signals = (await session.execute(
            select(CryptoSignalLog)
            .where(CryptoSignalLog.timestamp >= cutoff,
                   CryptoSignalLog.current_price > 0)
            .order_by(CryptoSignalLog.timestamp))).scalars().all()

    # Bad prints out before anything is replayed, using the SAME rule the
    # research pass uses rather than a second copy of it.
    #
    # This is not a precaution, it is the correction to a published mistake.
    # A first run of this analysis skipped the filter, and gold's 55 snapshots
    # priced at 4.3e-05 instead of 4350 turned a p of 0.02 into 0.81 — which
    # is the difference between "the detectors are doing something" and "the
    # detectors are worthless". The filter already existed; only the reuse
    # was missing, so there is exactly one of it now.
    from analysis.research_report import _plausible_price_band

    band = _plausible_price_band(snaps)
    paths: dict[str, tuple[list, list]] = {}
    for row in snaps:
        low, high = band.get(row.symbol, (0.0, float("inf")))
        if low <= row.price <= high:
            times, prices = paths.setdefault(row.symbol, ([], []))
            times.append(row.timestamp)
            prices.append(row.price)
    dropped = len(snaps) - sum(len(t) for t, _ in paths.values())
    if dropped:
        print(f"  dropped {dropped} implausible prices before replaying")

    entries = [Entry(s.symbol, s.direction, s.timestamp) for s in signals]
    return entries, paths


async def main(args) -> int:
    entries, paths = await load(args.days)
    if not entries:
        print(f"\n  No signals in the last {args.days} days. Nothing to test.\n")
        return 1
    if not paths:
        print("\n  No usable price history. Restore or collect snapshots first.\n")
        return 1

    cost = spec_for(next(iter(paths))).round_trip_pct * 100
    print(f"\n  {len(entries)} signals, {sum(len(t) for t, _ in paths.values()):,} price points, "
          f"{len(paths)} symbols")
    print(f"  round trip {cost:.4f}%\n")
    print(render(compare_against_random(
        entries, paths, args.stop, args.target, args.hold, cost, args.trials), 
        args.stop, args.target, args.hold))
    print("\n  A detector that cannot beat this is not yet a detector. One week of")
    print("  one regime can miss an edge that exists; it cannot manufacture one.\n")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stop", type=float, default=0.92, help="Stop, %% of price (default 0.92)")
    p.add_argument("--target", type=float, default=1.84, help="Target, %% of price")
    p.add_argument("--hold", type=float, default=24.0, help="Hold, hours")
    p.add_argument("--days", type=int, default=120, help="How far back to read")
    p.add_argument("--trials", type=int, default=200, help="Random replays")
    raise SystemExit(asyncio.run(main(p.parse_args())))
