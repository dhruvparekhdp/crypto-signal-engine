"""
Backtest the v2 setups on the Binance lake and say whether any may go to paper.

Needs the lake (scripts/load_binance_lake.py) with 5m, 15m, 4h and 1d klines
and fundingRate for each symbol — the loader's defaults include all of them.

    python -m scripts.backtest_v2                              # watchlist defaults, 2 years
    python -m scripts.backtest_v2 --symbols BTCUSDT,ETHUSDT,SOLUSDT --years 2
    python -m scripts.backtest_v2 --setups A,B --no-session    # variants: count them!

Prints, per setup and overall: trades, win rate, expectancy in R after all
costs, profit factor, drawdown, walk-forward windows, Monte Carlo, and the
promotion gates. Writes the full report and every trade to
data/reports/backtest_v2_{timestamp}.json for the local model and pandas.

Every variant you run is another chance to fool yourself: the report counts
the runs in data/reports so the number of variants tried is on record.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from analysis.v2_backtest import ExecConfig, grade, simulate, trades_to_rows
from analysis.v2_setups import SETUP_NAMES, V2Config, generate
from collectors.binance_lake import read

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "LTCUSDT", "BCHUSDT"]


def _line(name: str, g: dict) -> str:
    s = g["stats"]
    if not s.get("trades"):
        return f"  {name:22} no trades"
    pf = s["profit_factor"]
    gates = " ".join(f"{k}:{'ok' if v else 'NO'}" for k, v in g["gates"].items())
    return (f"  {name:22} {s['trades']:5} trades  win {s['win_rate'] * 100:5.1f}%  "
            f"exp {s['expectancy_r']:+.3f}R (taxed {s['expectancy_after_tax_r']:+.3f}R)  PF {pf if pf is not None else '-':>5}  "
            f"DD {s['max_drawdown_r']:6.1f}R  windows+ {g['positive_windows'] * 100:4.0f}%"
            f"  | {gates}  => {'PROMOTE' if g['promote_to_paper'] else 'hold'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--setups", default="A,B,C,D")
    ap.add_argument("--no-session", action="store_true", help="Trade all hours and days")
    ap.add_argument("--root", default="data/lake")
    ap.add_argument("--reports", default="data/reports")
    args = ap.parse_args()

    end = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    start = end - pd.Timedelta(days=int(args.years * 365))
    cfg = V2Config(setups=tuple(s.strip().upper() for s in args.setups.split(",")),
                   session_filter=not args.no_session)
    ex = ExecConfig()

    all_trades = []
    for sym in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        warm = start - pd.Timedelta(days=40)    # indicators need history before the window
        frames = {iv: read("klines", sym, warm, end, interval=iv, root=args.root)
                  for iv in ("5m", "15m", "4h", "1d")}
        if frames["5m"].empty or frames["15m"].empty:
            print(f"{sym}: no 5m/15m klines in the lake, run load_binance_lake first")
            continue
        funding = read("fundingRate", sym, warm, end, root=args.root)
        cands = [c for c in generate(sym.lower(), frames["5m"], frames["15m"], frames["4h"],
                                     frames["1d"], funding, cfg) if c.ts >= start]
        trades = simulate(cands, frames["5m"], ex)
        all_trades += trades
        print(f"{sym}: {len(cands)} candidates, {len(trades)} filled trades")

    print(f"\nv2 backtest {start.date()} -> {end.date()}, costs: maker {ex.maker * 100:.4f}% "
          f"taker {ex.taker * 100:.4f}% (GST incl.), stop slip {ex.stop_slip * 100:.2f}%")
    report = {"generated": datetime.now(UTC).isoformat(), "args": vars(args),
              "config": {k: (list(v) if isinstance(v, tuple) else v)
                         for k, v in replace(cfg).__dict__.items()},
              "setups": {}}
    for code in cfg.setups:
        sub = [t for t in all_trades if t.setup == code]
        g = grade(sub)
        report["setups"][code] = g
        print(_line(f"{code} {SETUP_NAMES[code]}", g))
    g = grade(all_trades)
    report["overall"] = g
    print(_line("ALL", g))
    if g["monte_carlo"]:
        mc = g["monte_carlo"]
        print(f"\n  Monte Carlo at {mc['risk_pct_per_trade']}% risk/trade: median final "
              f"x{mc['final_equity_p50']}, 5th percentile x{mc['final_equity_p5']}, "
              f"95th-percentile drawdown {mc['max_drawdown_p95_pct']}%")

    out = Path(args.reports)
    out.mkdir(parents=True, exist_ok=True)
    runs = len(list(out.glob("backtest_v2_*.json"))) + 1
    report["variants_tried_so_far"] = runs
    report["trades"] = trades_to_rows(all_trades)
    path = out / f"backtest_v2_{datetime.now(UTC):%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(report, default=str))
    print(f"\nrun #{runs} on record; report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
