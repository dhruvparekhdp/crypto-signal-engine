"""
One v2 backtest run over the lake, as a report dict. Used by
scripts/backtest_v2.py and by the server's scheduled backtest.

The report holds, for the whole book, each setup, each coin and each side:
trades, win rate, expectancy in R after all costs, profit factor, drawdown,
walk-forward windows, Monte Carlo and the promotion gates. Win rate alone
is not the target — a 35% win rate at 2.5R wins beats 60% at 0.5R — so
every row carries expectancy beside it.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pandas as pd

from analysis.v2_backtest import ExecConfig, grade, simulate, trades_to_rows
from analysis.v2_setups import SETUP_NAMES, V2Config, generate
from collectors.binance_lake import read

OHLCV = ["ts", "open", "high", "low", "close", "volume"]


def _small(g: dict) -> dict:
    """A grade without the per-window list, for compact breakdowns."""
    return {"stats": g["stats"], "gates": g["gates"],
            "positive_windows": g["positive_windows"],
            "promote_to_paper": g["promote_to_paper"]}


def run_backtest(symbols: list[str], years: float = 2.0, cfg: V2Config = V2Config(),
                 ex: ExecConfig = ExecConfig(), root: str = "data/lake",
                 log=print) -> dict:
    end = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    start = end - pd.Timedelta(days=int(years * 365))
    warm = start - pd.Timedelta(days=40)     # indicators need history before the window
    all_trades, per_symbol, missing = [], {}, []
    for sym in symbols:
        sym = sym.upper()
        # Only the columns the setups use: the archive's other seven columns
        # more than doubled the memory of two years of 5m bars.
        frames = {iv: read("klines", sym, warm, end, interval=iv, root=root,
                           columns=OHLCV) for iv in ("5m", "15m", "4h", "1d")}
        if frames["5m"].empty or frames["15m"].empty:
            missing.append(sym)
            log(f"{sym}: no 5m/15m klines in the lake")
            continue
        funding = read("fundingRate", sym, warm, end, root=root,
                       columns=["ts", "last_funding_rate"])
        cands = [c for c in generate(sym.lower(), frames["5m"], frames["15m"], frames["4h"],
                                     frames["1d"], funding, cfg) if c.ts >= start]
        trades = simulate(cands, frames["5m"], ex)
        all_trades += trades
        per_symbol[sym] = _small(grade(trades, mc=False))
        log(f"{sym}: {len(cands)} candidates, {len(trades)} filled trades")
        del frames, funding, cands

    report = {
        "generated": datetime.now(UTC).isoformat(),
        "window": [str(start.date()), str(end.date())],
        "symbols": [s.upper() for s in symbols], "missing": missing,
        "costs": {"maker_pct": ex.maker * 100, "taker_pct": ex.taker * 100,
                  "stop_slip_pct": ex.stop_slip * 100},
        "config": {k: (list(v) if isinstance(v, tuple) else v)
                   for k, v in replace(cfg).__dict__.items()},
        "setups": {c: dict(grade([t for t in all_trades if t.setup == c]),
                           name=SETUP_NAMES[c]) for c in cfg.setups},
        "by_symbol": per_symbol,
        "by_side": {s: _small(grade([t for t in all_trades if t.side == s], mc=False))
                    for s in ("long", "short")},
        "by_setup_side": {f"{c}_{s}": _small(grade([t for t in all_trades
                                                   if t.setup == c and t.side == s],
                                                  mc=False))
                          for c in cfg.setups for s in ("long", "short")},
        "overall": grade(all_trades),
        "trades": trades_to_rows(all_trades),
    }
    return report
