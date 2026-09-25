"""
Mine the Binance lake for past moves, measure what came before them, and have
a model label them. Load the lake first (scripts/load_binance_lake.py).

    # 1. Find every large hourly move and store it with its facts. Free.
    python -m scripts.review_history scan --z 3

    # 2. Label them, biggest first. Local model for the bulk; Groq with web
    #    search for moves of 5+ standard deviations. Stops at --max-calls so
    #    live trading keeps its share of the free Groq requests.
    python -m scripts.review_history review --max-calls 150 --search-z 5

    # 3. Which bar size can pay its fees, per coin and quarter
    python -m scripts.review_history timeframes --years 2

    # 4. Everything labelled, as JSON lines for the local model or pandas
    python -m scripts.review_history export > history_events.jsonl

Run `review` daily from cron; it picks up where it stopped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import pandas as pd

from analysis.history_review import (
    REVIEW_SYSTEM,
    facts_before,
    notable_moves,
    outcome_after,
    parse_review,
    pick_timeframe,
    review_prompt,
    timeframe_costs,
)
from collectors.binance_lake import read

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "LTCUSDT", "BCHUSDT"]


async def _symbols(arg: str) -> list[str]:
    if arg:
        return [s.strip().upper() for s in arg.split(",") if s.strip()]
    try:
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        async with AsyncSessionFactory() as session:
            syms = await Repository(session).get_crypto_watchlist()
        return [s.upper() for s in syms if s.lower().endswith("usdt")] or DEFAULT_SYMBOLS
    except Exception:
        return DEFAULT_SYMBOLS


def _span(years: float):
    end = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    return end - pd.Timedelta(days=int(years * 365)), end


async def scan(args) -> int:
    from storage.database import AsyncSessionFactory, init_db
    from storage.repository import Repository

    await init_db()
    start, end = _span(args.years)
    total = 0
    for sym in await _symbols(args.symbols):
        k1h = read("klines", sym, start, end, interval="1h", root=args.root)
        if k1h.empty:
            print(f"{sym}: no 1h klines in the lake, run load_binance_lake first")
            continue
        k15 = read("klines", sym, start, end, interval="15m", root=args.root)
        metrics = read("metrics", sym, start, end, root=args.root)
        funding = read("fundingRate", sym, start, end, root=args.root)
        moves = notable_moves(k1h, z=args.z, min_pct=args.min_pct)
        events = []
        for m in moves.itertuples(index=False):
            at = pd.Timestamp(m.ts)
            events.append({
                "at": at.to_pydatetime(), "ret_pct": round(float(m.ret_pct), 3),
                "z": round(float(m.z), 2), "direction": m.direction,
                "facts": facts_before(at, k15, k1h, metrics, funding),
                "after": outcome_after(at, k1h)})
        async with AsyncSessionFactory() as session:
            new = await Repository(session).save_history_events(sym, events)
        total += new
        print(f"{sym}: {len(events)} moves of {args.z}+ sd, {new} new")
    print(f"stored {total} new events")
    return 0


async def review(args) -> int:
    from collectors.llm_client import ask_json, chain_for
    from storage.database import AsyncSessionFactory, init_db
    from storage.repository import Repository

    await init_db()
    if not chain_for("history") and not chain_for("history_search"):
        print("no model configured for history review (llm_chain_history)", file=sys.stderr)
        return 1
    calls = done = 0
    while calls < args.max_calls:
        async with AsyncSessionFactory() as session:
            batch = await Repository(session).unreviewed_history_events(
                min(25, args.max_calls - calls))
        if not batch:
            break
        for row in batch:
            event = {"at": row.at.strftime("%Y-%m-%d %H:%M"), "ret_pct": row.ret_pct,
                     "z": row.zscore, "facts": json.loads(row.facts or "{}"),
                     "after": json.loads(row.after or "{}")}
            role = ("history_search" if abs(row.zscore) >= args.search_z
                    and chain_for("history_search") else "history")
            calls += 1
            reply = await ask_json(role, REVIEW_SYSTEM, review_prompt(row.symbol, event),
                                   max_tokens=1200, temperature=0.2, timeout=120.0)
            if not reply:
                print(f"  no answer for {row.symbol} {event['at']}: {reply.failures[:2]}",
                      file=sys.stderr)
                if calls >= args.max_calls:
                    break
                continue
            label = parse_review(reply.data)
            async with AsyncSessionFactory() as session:
                await Repository(session).save_history_review(row.id, label, reply.served_by)
            done += 1
            print(f"{row.symbol} {event['at']} {row.ret_pct:+.2f}% -> {label['move_type']}, "
                  f"setup {label['setup']}, visible {label['visible_before']} "
                  f"({reply.served_by})")
            if calls >= args.max_calls:
                break
    print(f"labelled {done} events in {calls} calls")
    return 0


async def timeframes(args) -> int:
    start, end = _span(args.years)
    for sym in await _symbols(args.symbols):
        k1m = read("klines", sym, start, end, interval="1m", root=args.root,
                   columns=["ts", "high", "low", "close"])
        if k1m.empty:
            print(f"{sym}: no 1m klines in the lake")
            continue
        print(f"\n{sym}  (round trip {args.cost:.3f}%, need {args.need}x)")
        for q, part in k1m.groupby(k1m["ts"].dt.to_period("Q")):
            costs = timeframe_costs(part, args.cost)
            row = "  ".join(f"{c['timeframe']}:{c['cost_multiple']:.1f}x" for c in costs)
            print(f"  {q}  smallest viable: {pick_timeframe(costs, args.need) or 'none':6}  {row}")
    return 0


async def export(args) -> int:
    from sqlalchemy import select

    from storage.database import AsyncSessionFactory
    from storage.models import HistoryEvent

    async with AsyncSessionFactory() as session:
        res = await session.execute(select(HistoryEvent).order_by(HistoryEvent.at))
        for r in res.scalars():
            if args.reviewed_only and not r.review:
                continue
            print(json.dumps({
                "symbol": r.symbol, "at": r.at.isoformat(), "ret_pct": r.ret_pct,
                "z": r.zscore, "facts": json.loads(r.facts or "{}"),
                "after": json.loads(r.after or "{}"),
                "review": json.loads(r.review) if r.review else None,
                "reviewed_by": r.reviewed_by}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="data/lake")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--symbols", default="")
    s.add_argument("--years", type=float, default=5.0)
    s.add_argument("--z", type=float, default=3.0)
    s.add_argument("--min-pct", type=float, default=1.0)
    r = sub.add_parser("review")
    r.add_argument("--max-calls", type=int, default=150)
    r.add_argument("--search-z", type=float, default=5.0,
                   help="Moves this large use the web-search chain")
    t = sub.add_parser("timeframes")
    t.add_argument("--symbols", default="")
    t.add_argument("--years", type=float, default=2.0)
    t.add_argument("--cost", type=float, default=0.118, help="Round-trip cost, percent")
    t.add_argument("--need", type=float, default=3.0)
    e = sub.add_parser("export")
    e.add_argument("--reviewed-only", action="store_true")
    args = ap.parse_args()
    return asyncio.run({"scan": scan, "review": review, "timeframes": timeframes,
                        "export": export}[args.cmd](args))


if __name__ == "__main__":
    raise SystemExit(main())
