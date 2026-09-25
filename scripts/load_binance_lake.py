"""
Load Binance's public history into the local Parquet lake (collectors/binance_lake.py).

No API key, no rate-limited REST: data.binance.vision serves zipped CSV per
symbol per month (or per day), each with a published SHA-256. Resumable —
every fetched or missing file is recorded in a `_done.txt` beside the data,
and a re-run fetches only what is new. Run it daily from cron to stay current.

Examples (from the repo root):

    # See the file count and disk size first
    python -m scripts.load_binance_lake --dry-run

    # 5 years of USDT-M futures candles 1m..1d, OI/long-short metrics, funding
    python -m scripts.load_binance_lake --years 5

    # Seconds: spot 1s klines for BTC and ETH, last 90 days (about 2 GB)
    python -m scripts.load_binance_lake --market spot --kinds klines --intervals 1s \\
        --symbols BTCUSDT,ETHUSDT --days 90

    # Every trade (very large — BTC is ~15 GB/year even compressed)
    python -m scripts.load_binance_lake --kinds aggTrades --symbols SOLUSDT --days 30

Default symbols are the engine's watchlist when the database is reachable,
otherwise the seven the engine has always traded.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import httpx

from collectors.binance_lake import (
    INTERVAL_KINDS,
    LAKE,
    Part,
    estimate_mb,
    parse_csv,
    plan,
    unzip_verified,
    write_month,
)

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "LTCUSDT", "BCHUSDT"]


async def _watchlist() -> list[str]:
    try:
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        async with AsyncSessionFactory() as session:
            syms = await Repository(session).get_crypto_watchlist()
        return [s.upper() for s in syms if s.lower().endswith("usdt")] or DEFAULT_SYMBOLS
    except Exception:
        return DEFAULT_SYMBOLS


def _done_file(root: Path, p: Part) -> Path:
    return root / p.market / p.kind / p.symbol / (p.interval or "-") / "_done.txt"


def _done(root: Path, p: Part) -> set[str]:
    f = _done_file(root, p)
    return set(f.read_text().split()) if f.exists() else set()


def _mark(root: Path, p: Part, periods: list[str]) -> None:
    f = _done_file(root, p)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write("".join(f"{x}\n" for x in periods))


async def _get(client: httpx.AsyncClient, url: str) -> bytes | None:
    """The body, None on 404 (not published), raising after retries otherwise."""
    for attempt in range(5):
        try:
            r = await client.get(url)
        except httpx.HTTPError:
            await asyncio.sleep(2 ** attempt)
            continue
        if r.status_code == 404:
            return None
        if r.status_code == 200:
            return r.content
        await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"gave up on {url}")


async def _fetch(client, sem, part: Part):
    async with sem:
        blob = await _get(client, part.url)
        if blob is None:
            return part, None
        checksum = await _get(client, part.url + ".CHECKSUM")
        csv = unzip_verified(blob, checksum.decode() if checksum else None)
        return part, parse_csv(part.kind, csv)


async def load(args) -> int:
    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               or await _watchlist())
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    intervals = [i.strip() for i in args.intervals.split(",") if i.strip()]
    end = date.today() - timedelta(days=1)
    start = (date.fromisoformat(args.since) if args.since
             else end - timedelta(days=args.days or int(args.years * 365)))
    root = Path(args.root)

    series = []
    for sym in symbols:
        for kind in kinds:
            for iv in (intervals if kind in INTERVAL_KINDS else [""]):
                try:
                    parts = plan(args.market, kind, sym, iv, start, end)
                except ValueError as exc:
                    print(f"skip {sym} {kind} {iv}: {exc}")
                    continue
                series.append((sym, kind, iv, parts))

    years = (end - start).days / 365
    total_files = sum(len(p) for *_, p in series)
    total_mb = sum(estimate_mb(k, i, years, 1) for _, k, i, _ in series)
    print(f"{len(symbols)} symbols, {start} -> {end}, {len(series)} series, "
          f"{total_files} files, roughly {total_mb / 1024:.1f} GB on disk")
    if args.dry_run:
        for sym, kind, iv, parts in series:
            print(f"  {sym:10} {kind:18} {iv or '-':4} {len(parts):5} files  "
                  f"~{estimate_mb(kind, iv, years, 1):8.1f} MB")
        return 0

    failures = 0
    limits = httpx.Limits(max_connections=args.concurrency)
    async with httpx.AsyncClient(timeout=120.0, limits=limits, follow_redirects=True) as client:
        sem = asyncio.Semaphore(args.concurrency)
        for sym, kind, iv, parts in series:
            if not parts:
                continue
            done = _done(root, parts[0])
            todo = [p for p in parts if p.period not in done]
            if not todo:
                continue
            by_month: dict[str, list[Part]] = defaultdict(list)
            for p in todo:
                by_month[p.month].append(p)
            got_rows = 0
            for month, mparts in sorted(by_month.items()):
                results = await asyncio.gather(*(_fetch(client, sem, p) for p in mparts),
                                               return_exceptions=True)
                frames, finished = [], []
                recent = (end - timedelta(days=3)).isoformat()
                for r in results:
                    if isinstance(r, Exception):
                        failures += 1
                        print(f"  ! {sym} {kind} {iv} {month}: {r}", file=sys.stderr)
                        continue
                    part, df = r
                    if df is not None:
                        frames.append(df)
                        got_rows += len(df)
                        finished.append(part.period)
                    elif not (part.daily and part.period >= recent):
                        # Missing and old: the pair was not listed then. A
                        # missing file from the last few days may still be
                        # published, so it is tried again next run.
                        finished.append(part.period)
                write_month(root, mparts[0], frames)
                _mark(root, mparts[0], finished)
            print(f"{sym:10} {kind:18} {iv or '-':4} +{got_rows:,} rows")
    if failures:
        print(f"{failures} files failed; re-run to retry them", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--market", default="um", choices=["um", "spot"],
                    help="um = USDT-M futures (default), spot")
    ap.add_argument("--kinds", default="klines,metrics,fundingRate",
                    help="klines, metrics, fundingRate, bookDepth, aggTrades, trades, "
                         "markPriceKlines, premiumIndexKlines, indexPriceKlines")
    ap.add_argument("--intervals", default="1m,5m,15m,1h,4h,1d",
                    help="For kline kinds: 1s (spot only), 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, "
                         "6h, 8h, 12h, 1d, 3d, 1w, 1mo")
    ap.add_argument("--symbols", default="", help="Comma-separated; default the watchlist")
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--days", type=int, default=0, help="Overrides --years")
    ap.add_argument("--since", default="", help="YYYY-MM-DD; overrides --years/--days")
    ap.add_argument("--root", default=str(LAKE))
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    return asyncio.run(load(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
