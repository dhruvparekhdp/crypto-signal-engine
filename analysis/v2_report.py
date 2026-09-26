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

VARIANTS = {
    "base": ExecConfig(),
    "taker_entry": ExecConfig(entry_mode="taker"),
    "breakeven": ExecConfig(breakeven_at_r=1.25),
    "partial": ExecConfig(partial_at_r=1.5),
}
# Setup variants (research items 2-8): same execution, different filters.
SETUP_VARIANTS = {
    "premium_discount": ({"premium_discount": True}, "Buy low / sell high in the swing"),
    "htf_strict": ({"htf_strict": True}, "4h trend must agree"),
    "news_blackout": ({"news_blackout": True}, "No entries around FOMC / jobs report"),
    "clean_swings": ({"swing_alternate": True, "min_swing_atr": 0.75},
                     "Alternating swings, >= 0.75 ATR"),
    "displacement": ({"displacement_atr": 0.8}, "Breakout bar body >= 0.8 ATR (D)"),
    "deeper_entry": ({"entry_depth": 0.25}, "Limit 25% deeper into the trigger bar"),
    "regime": ({"regime_routing": True}, "Trend setups in trends, range in ranges"),
    "all_filters": ({"premium_discount": True, "htf_strict": True, "news_blackout": True,
                     "swing_alternate": True, "min_swing_atr": 0.75, "regime_routing": True},
                    "All filters together (not entry depth)"),
}
TRIALS = 4 + len(SETUP_VARIANTS)       # execution variants + setup variants

VARIANT_LABELS = {
    "base": "Limit entry, full target",
    "taker_entry": "Market entry at next open",
    "breakeven": "Limit entry, stop to breakeven at +1.25R",
    "partial": "Limit entry, half off at +1.5R, rest to target",
}


def _small(g: dict) -> dict:
    """A grade without the per-window list, for compact breakdowns."""
    return {"stats": g["stats"], "gates": g["gates"],
            "positive_windows": g["positive_windows"],
            "promote_to_paper": g["promote_to_paper"]}


def _bench_summary(bench: dict) -> dict:
    from analysis.ft_bench import STRATEGIES, summarise
    return {st.name: dict(summarise(bench.get(st.name, [])), timeframe=st.timeframe,
                          source=st.source) for st in STRATEGIES}


def run_backtest(symbols: list[str], years: float = 2.0, cfg: V2Config = V2Config(),
                 ex: ExecConfig = ExecConfig(), root: str = "data/lake",
                 log=print, benchmarks: bool = True, setup_variants: bool = True) -> dict:
    end = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    start = end - pd.Timedelta(days=int(years * 365))
    warm = start - pd.Timedelta(days=40)     # indicators need history before the window
    all_trades, per_symbol, missing = [], {}, []
    from collections import defaultdict
    bench: dict[str, list] = defaultdict(list)
    variant_trades: dict[str, list] = defaultdict(list)
    setup_trades: dict[str, list] = defaultdict(list)
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
        for name, vex in VARIANTS.items():
            variant_trades[name] += (trades if name == "base"
                                     else simulate(cands, frames["5m"], vex))
        if setup_variants:
            for name, (flags, _) in SETUP_VARIANTS.items():
                vcfg = replace(cfg, **flags)
                vc = [c for c in generate(sym.lower(), frames["5m"], frames["15m"],
                                          frames["4h"], frames["1d"], funding, vcfg)
                      if c.ts >= start]
                setup_trades[name] += simulate(vc, frames["5m"], ex)
        if benchmarks:
            from analysis.ft_bench import STRATEGIES, resample, run_strategy
            for st in STRATEGIES:
                df = resample(frames["15m"], "1h") if st.timeframe == "1h" \
                    else frames[st.timeframe]
                bench[st.name] += [t for t in run_strategy(st, df, fee=ex.taker,
                                                           stop_slip=ex.stop_slip)
                                   if pd.Timestamp(t["entry_at"]) >= start]
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
        "overall": grade(all_trades, n_trials=TRIALS),
        # The same candidates under each execution variant: what the limit
        # entry costs (vs taker), and what breakeven / partial exits do to
        # win rate AND expectancy. Every variant counts as a trial.
        "variants": {name: {
            "label": VARIANT_LABELS[name],
            "overall": _small(grade(vt, mc=False, n_trials=TRIALS)),
            "setups": {c: grade([t for t in vt if t.setup == c], mc=False,
                                n_trials=TRIALS)["stats"] for c in cfg.setups}}
            for name, vt in variant_trades.items()},
        # One research filter at a time (and all together), base execution.
        "setup_variants": {name: {
            "label": SETUP_VARIANTS[name][1],
            "overall": _small(grade(vt, mc=False, n_trials=TRIALS)),
            "setups": {c: grade([t for t in vt if t.setup == c], mc=False,
                                n_trials=TRIALS)["stats"] for c in cfg.setups}}
            for name, vt in setup_trades.items()},
        # Freqtrade community strategies on the same data and costs (1x spot,
        # long-only, percent per trade): the bar v2 has to clear.
        "benchmarks": _bench_summary(bench),
        "trades": trades_to_rows(all_trades),
    }
    return report
