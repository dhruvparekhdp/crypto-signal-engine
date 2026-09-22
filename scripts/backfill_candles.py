"""
Fill market_candles from Binance. Safe to re-run, interrupt, and re-run again.

    # see what it would cost before committing to it
    python scripts/backfill_candles.py --years 2 --interval 5m --dry-run

    # do it
    python scripts/backfill_candles.py --years 2 --interval 5m

    # just the tail, e.g. from cron
    python scripts/backfill_candles.py --days 2 --interval 1m

    # what is actually stored
    python scripts/backfill_candles.py --status

No API key. Binance market data is public, and the bulk archives this reads
are plain HTTP.

Re-running is the normal case, not the exception. The unique constraint on
(symbol, interval, open_time) means an overlapping range inserts only what is
genuinely new, so there is no bookkeeping to get wrong: if a run dies halfway,
run it again with the same arguments.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collectors.binance_history import (  # noqa: E402
    INTERVAL_SECONDS,
    BinanceHistory,
    BinanceUnreachable,
    expected_bars,
)
from config.settings import settings  # noqa: E402
from storage.database import AsyncSessionFactory, init_db  # noqa: E402
from storage.repository import Repository  # noqa: E402

# Roughly what one row costs on Postgres once the index is counted. Measured
# rather than guessed: 13 columns, most of them float8, plus a three-column
# btree. Used only for the estimate, so being 20% out is harmless — being an
# order of magnitude out would not be, which is why it is not a round number.
BYTES_PER_ROW = 150


async def _watchlist() -> list[str]:
    """
    Symbols come from the live watchlist, falling back to the configured
    default. Backfilling something that is not traded is a waste of disk.
    """
    try:
        async with AsyncSessionFactory() as session:
            symbols = [s.lower() for s in
                       await Repository(session).get_crypto_watchlist() if s]
            if symbols:
                return symbols
    except Exception:
        pass
    return [s.strip().lower() for s in settings.crypto_watchlist_seed.split(",") if s.strip()]


def _estimate(symbols: list[str], interval: str, start: datetime, end: datetime) -> None:
    per = expected_bars(start, end, interval)
    total = per * len(symbols)
    mb = total * BYTES_PER_ROW / 1_000_000
    print(f"  {len(symbols)} symbols × {per:,} bars = {total:,} rows")
    print(f"  roughly {mb:,.0f} MB on disk"
          + ("   <-- check the database has room" if mb > 500 else ""))


async def status() -> int:
    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        symbols = await _watchlist()
        print(f"\n{'symbol':<12}{'interval':<10}{'held':>10}{'gaps':>9}  range")
        print("-" * 78)
        anything = False
        for symbol in symbols:
            for interval in sorted(INTERVAL_SECONDS, key=lambda i: INTERVAL_SECONDS[i]):
                cov = await repo.candle_coverage(symbol, interval)
                if not cov["held"]:
                    continue
                anything = True
                print(f"{symbol:<12}{interval:<10}{cov['held']:>10,}{cov['gaps']:>9,}"
                      f"  {cov['first'][:16]} .. {cov['last'][:16]}")
        if not anything:
            print("  nothing stored yet — run without --status to fill it")
        print()
    return 0


async def backfill(symbols: list[str], interval: str, start: datetime,
                   end: datetime, dry_run: bool) -> int:
    print(f"\nBinance candles · {interval} · {start:%Y-%m-%d} to {end:%Y-%m-%d}")
    _estimate(symbols, interval, start, end)

    if dry_run:
        print("\n  --dry-run: nothing fetched, nothing written.\n")
        return 0

    print()
    grand_new = grand_seen = 0
    unreachable = 0

    for symbol in symbols:
        # A fresh fetcher per symbol so one symbol's failure count does not
        # colour the next one's verdict.
        history = BinanceHistory()
        seen = new = 0
        try:
            async for batch in history.fetch_range(symbol, interval, start, end):
                seen += len(batch)
                async with AsyncSessionFactory() as session:
                    new += await Repository(session).save_candles(batch)
        except BinanceUnreachable as exc:
            unreachable += 1
            print(f"  {symbol:<10} UNREACHABLE — {exc}")
            continue
        except Exception as exc:
            # One symbol failing must not cost the other six. The range is
            # re-runnable, so the recovery is to run it again.
            print(f"  {symbol:<10} FAILED after {new:,} rows: {str(exc)[:120]}")
            continue

        async with AsyncSessionFactory() as session:
            cov = await Repository(session).candle_coverage(symbol, interval)
        dupes = seen - new
        print(f"  {symbol:<10} fetched {seen:>8,}  new {new:>8,}  "
              f"already had {dupes:>8,}  now holding {cov['held']:,} "
              f"({cov['gaps']:,} gaps)")
        grand_new += new
        grand_seen += seen

    if unreachable == len(symbols):
        print(f"\n  Nothing was fetched: every one of the {unreachable} symbols was "
              "unreachable.")
        print("  This is a network problem, not an empty range. Check that this host")
        print("  can reach data.binance.vision and api.binance.com.\n")
        return 1

    print(f"\n  {grand_new:,} new rows from {grand_seen:,} fetched.")
    if unreachable:
        print(f"  {unreachable} symbol(s) unreachable — re-run to pick them up.")
    if grand_seen and grand_new < grand_seen:
        print("  The difference is bars already stored — that is the dedup working,")
        print("  not an error. Re-running this command is always safe.\n")
    else:
        print()
    return 0


async def main(args) -> int:
    await init_db()
    if args.status:
        return await status()

    if args.interval not in INTERVAL_SECONDS:
        print(f"Unknown interval {args.interval!r}. "
              f"Known: {', '.join(sorted(INTERVAL_SECONDS, key=lambda i: INTERVAL_SECONDS[i]))}")
        return 2

    end = datetime.now(UTC).replace(tzinfo=None, second=0, microsecond=0)
    days = args.days if args.days else int(args.years * 365)
    start = end - timedelta(days=days)

    symbols = [s.lower() for s in args.symbols.split(",") if s.strip()] \
        if args.symbols else await _watchlist()
    if not symbols:
        print("No symbols. Pass --symbols btcusdt,ethusdt or populate the watchlist.")
        return 2

    return await backfill(symbols, args.interval, start, end, args.dry_run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", default="1m", help="Bar size (default 1m)")
    parser.add_argument("--years", type=float, default=1.0, help="How far back (default 1)")
    parser.add_argument("--days", type=int, default=0,
                        help="How far back, in days. Wins over --years.")
    parser.add_argument("--symbols", default="", help="Comma-separated; default is the watchlist")
    parser.add_argument("--dry-run", action="store_true", help="Print the size estimate and stop")
    parser.add_argument("--status", action="store_true", help="Show what is already stored")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
