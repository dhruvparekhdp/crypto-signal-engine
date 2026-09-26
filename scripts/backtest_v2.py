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
from datetime import UTC, datetime
from pathlib import Path

from analysis.v2_backtest import ExecConfig
from analysis.v2_report import run_backtest
from analysis.v2_setups import SETUP_NAMES, V2Config

DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "LTCUSDT", "BCHUSDT"]


def _line(name: str, g: dict) -> str:
    s = g["stats"]
    if not s.get("trades"):
        return f"  {name:22} no trades"
    pf = s["profit_factor"]
    gates = " ".join(f"{k}:{'ok' if v else 'NO'}" for k, v in g["gates"].items())
    return (f"  {name:22} {s['trades']:5} trades  win {s['win_rate'] * 100:5.1f}%  "
            f"exp {s['expectancy_r']:+.3f}R  avg win {s['avg_win_r']:+.2f}R  "
            f"avg loss {s['avg_loss_r']:+.2f}R  "
            f"PF {pf if pf is not None else '-':>5}  "
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
    ap.add_argument("--download", action="store_true",
                    help="First fetch what is missing from data.binance.vision")
    ap.add_argument("--latest", action="store_true",
                    help="Also write backtest_v2_latest.json (what /v2 shows)")
    args = ap.parse_args()

    if args.download:
        import asyncio
        from argparse import Namespace

        from scripts.load_binance_lake import load
        asyncio.run(load(Namespace(
            market="um", kinds="klines,fundingRate", intervals="5m,15m,4h,1d",
            symbols=args.symbols, years=args.years + 0.15, days=0, since="",
            root=args.root, concurrency=4, dry_run=False)))

    cfg = V2Config(setups=tuple(s.strip().upper() for s in args.setups.split(",")),
                   session_filter=not args.no_session)
    ex = ExecConfig()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    report = run_backtest(symbols, args.years, cfg, ex, root=args.root)
    report["args"] = vars(args)

    print(f"\nv2 backtest {report['window'][0]} -> {report['window'][1]}, costs: maker "
          f"{ex.maker * 100:.4f}% taker {ex.taker * 100:.4f}% (GST incl.), "
          f"stop slip {ex.stop_slip * 100:.2f}%")
    for code, g in report["setups"].items():
        print(_line(f"{code} {SETUP_NAMES[code]}", g))
    for side, g in report["by_side"].items():
        print(_line(f"  {side}s", g))
    for sym, g in report["by_symbol"].items():
        print(_line(f"  {sym}", g))
    g = report["overall"]
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
    path = out / f"backtest_v2_{datetime.now(UTC):%Y%m%d_%H%M%S}.json"
    text = json.dumps(report, default=str)
    path.write_text(text)
    if args.latest:
        (out / "backtest_v2_latest.json").write_text(text)
    print(f"\nrun #{runs} on record; report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
