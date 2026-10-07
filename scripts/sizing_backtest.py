"""Backtest the 7 Oct sizing changes: old production sizing vs margin cap + Binance order rules, by wallet size.

Replays the live swing book's trades (4h + 8h, volatility filter on, BCH/LTC excluded) through the production
sizing rule (analysis/swing_book.size) from a fresh wallet at the start of every month, for 12 months each.
Windows overlap: read the rates as "what if I had started then", not as independent trials.

    python -m scripts.sizing_backtest --out data/lab/sizing_backtest.json
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics

import numpy as np
import pandas as pd

from analysis.lab.wallet import WalletConfig, run_wallet

MONTH_MS = 30 * 86_400_000
RUNS = ["data/lab/runs/filt_vol_4h/trades.parquet", "data/lab/runs/filt_vol_8h/trades.parquet"]
EXCLUDE = {"BCHUSDT", "LTCUSDT"}

CONFIGS = {
    "old: 3% risk, no margin cap": dict(risk_pct=0.03, max_margin_frac=0.0, binance_rules=False),
    "new: 3% risk, cap 15% + Binance rules": dict(risk_pct=0.03, max_margin_frac=0.15, binance_rules=True),
    "new: 1% risk, cap 15% + Binance rules": dict(risk_pct=0.01, max_margin_frac=0.15, binance_rules=True),
    # Phase 1 account guards as in production: 9% daily loss limit, 2 h pause after 3 losses in a row
    "new 3% + Phase 1 guards": dict(risk_pct=0.03, max_margin_frac=0.15, binance_rules=True,
                                    daily_loss_pct=0.09, loss_streak_pause=3, pause_hours=2.0),
    "new 1% + Phase 1 guards": dict(risk_pct=0.01, max_margin_frac=0.15, binance_rules=True,
                                    daily_loss_pct=0.09, loss_streak_pause=3, pause_hours=2.0),
}


def load() -> pd.DataFrame:
    frames = []
    for p in RUNS:
        t = pd.read_parquet(p)
        t["tf"] = "8h" if "8h" in p else "4h"
        frames.append(t)
    t = pd.concat(frames, ignore_index=True)
    t = t[~t.symbol.isin(EXCLUDE)]
    return t.sort_values(["entry_t", "symbol"], kind="stable").reset_index(drop=True)


def replay(trades: pd.DataFrame, start: float, months: int, kw: dict) -> dict:
    cfg = WalletConfig(start=start, target=1e15, bust_below=start * 0.05, live_sizing=True, leverage=10.0,
                       max_concurrent=10_000, max_open_risk=0.09, reset=False, **kw)
    t_first, t_last = int(trades.entry_t.min()), int(trades.entry_t.max())
    ends, dds, n_tr, skips = [], [], [], {}
    t0 = t_first
    while t0 + months * MONTH_MS <= t_last:
        win = trades[(trades.entry_t >= t0) & (trades.entry_t < t0 + months * MONTH_MS)]
        if len(win) >= 5:
            res = run_wallet(win, dataclasses.replace(cfg))
            c = res.cycles[0]
            ends.append(c["end"] / start)
            dds.append(c["dd"])
            n_tr.append(c["trades"])
            for k, v in res.skipped.items():
                skips[k] = skips.get(k, 0) + v
        t0 += MONTH_MS
    tot = sum(n_tr) + sum(skips.values())
    return {"starts": len(ends), "median_end_x": round(statistics.median(ends), 2),
            "worst_end_x": round(min(ends), 2), "best_end_x": round(max(ends), 2),
            "pct_ended_up": round(float(np.mean([e > 1 for e in ends])) * 100),
            "pct_lost_half": round(float(np.mean([e < 0.5 for e in ends])) * 100),
            "median_max_dd_pct": round(statistics.median(dds) * 100),
            "trades_per_start": round(statistics.mean(n_tr)),
            "skipped_share_pct": round(100 * sum(skips.values()) / max(tot, 1)),
            "top_skips": dict(sorted(skips.items(), key=lambda kv: -kv[1])[:4])}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallets", default="29,50,100,300", help="starting balances in USDT (29 ~ Rs3,000)")
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    trades = load()
    print(f"{len(trades)} trades, {pd.to_datetime(trades.entry_t.min(), unit='ms'):%Y-%m} to "
          f"{pd.to_datetime(trades.entry_t.max(), unit='ms'):%Y-%m}, {a.months}-month windows started monthly\n")
    out = []
    for w in [float(x) for x in a.wallets.split(",")]:
        for name, kw in CONFIGS.items():
            r = replay(trades, w, a.months, kw)
            out.append({"wallet_usdt": w, "config": name, **r})
            print(f"${w:>5.0f}  {name:40} median x{r['median_end_x']:<5} worst x{r['worst_end_x']:<5} "
                  f"up {r['pct_ended_up']:>3}%  lost half {r['pct_lost_half']:>3}%  dd {r['median_max_dd_pct']:>3}%  "
                  f"trades {r['trades_per_start']:>4}  skipped {r['skipped_share_pct']:>3}% {r['top_skips']}")
        print()
    if a.out:
        with open(a.out, "w") as f:
            json.dump(out, f, indent=1)
        print("saved", a.out)


if __name__ == "__main__":
    main()
