"""The Rs500 Delta India trial, replayed (15 Oct 2026): live swing book (4h + 8h, volatility filter, BCH/LTC out,
profit ladder 1/1.5/2/2.5R with 0.25R buffer and 1h-close confirmation) sized the way execution/delta_book.py
sizes a live order: whole contracts of Delta India's real contract sizes, risk % of the available wallet,
<= 10x margin, at most N open, one per coin, and the loss guard (no new orders after -10% in a UTC day or below
the Rs350 floor).

    python -m scripts.delta_trial_backtest --out data/lab/delta_trial_backtest.json

Windows start every month and overlap ("what if I had started then"). In-sample for the strategies.
"""
from __future__ import annotations

import argparse
import json
import math

import numpy as np
import pandas as pd

from scripts.edge_report import load_run, tag_regimes

USD_INR = 85.0
LIVE_EXIT = "sl3.0_rr3.0_h10080_beNone_trNone_pNone_lad1:0-1.5:0.5-2:1-2.5:1.5_cf60_buf0.25"
# Delta India perpetual contract sizes (GET /v2/products, 15 Oct 2026), in coins per contract
CONTRACT = {"BTCUSDT": 0.001, "ETHUSDT": 0.01, "SOLUSDT": 1, "BNBUSDT": 0.1, "DOGEUSDT": 100, "XRPUSDT": 1,
            "ADAUSDT": 1, "AVAXUSDT": 1, "LINKUSDT": 1, "SUIUSDT": 1}
MONTH_MS = 30 * 86_400_000
DAY_MS = 86_400_000


def replay(t: pd.DataFrame, start_inr: float, risk_pct: float, max_open: int, daily_loss: float = 0.10,
           floor_inr: float = 350.0, max_lev: float = 10.0) -> dict:
    wallet = start_inr
    peak, mdd = wallet, 0.0
    open_pos: list[dict] = []          # {exit_t, symbol, pnl, margin}
    taken = skipped_size = skipped_full = skipped_guard = 0
    day, day_start = None, wallet
    stopped = False
    r_taken = []
    for row in t.itertuples(index=False):
        # settle everything that closed before this entry
        for p in sorted([p for p in open_pos if p["exit_t"] <= row.entry_t], key=lambda p: p["exit_t"]):
            wallet += p["pnl"]
            open_pos.remove(p)
            peak = max(peak, wallet)
            mdd = max(mdd, 1 - wallet / peak if peak > 0 else 0)
        d = row.entry_t // DAY_MS
        if d != day:
            day, day_start = d, wallet
        if wallet < floor_inr:
            stopped = True
            skipped_guard += 1
            continue
        if wallet < day_start * (1 - daily_loss):
            skipped_guard += 1
            continue
        if len(open_pos) >= max_open or any(p["symbol"] == row.symbol for p in open_pos):
            skipped_full += 1
            continue
        cv = CONTRACT.get(row.symbol)
        if cv is None:
            skipped_size += 1
            continue
        available = wallet - sum(p["margin"] for p in open_pos)
        per_contract_risk = cv * row.entry * row.stop_frac * USD_INR
        n = int(math.floor(available * risk_pct / per_contract_risk + 1e-12)) if per_contract_risk > 0 else 0
        cap = int((available / USD_INR) * max_lev * 0.9 // (cv * row.entry)) if row.entry > 0 else 0
        n = min(n, cap)
        if n < 1:
            skipped_size += 1
            continue
        notional_inr = n * cv * row.entry * USD_INR
        open_pos.append({"exit_t": row.exit_t, "symbol": row.symbol, "pnl": notional_inr * row.net_ret,
                         "margin": notional_inr / max_lev})
        taken += 1
        r_taken.append(row.r_net)
    for p in open_pos:
        wallet += p["pnl"]
    peak = max(peak, wallet)
    mdd = max(mdd, 1 - wallet / peak if peak > 0 else 0)
    return {"end_inr": round(wallet, 1), "x": round(wallet / start_inr, 3), "taken": taken,
            "skipped_too_small": skipped_size, "skipped_full": skipped_full, "skipped_guard": skipped_guard,
            "floor_hit": stopped, "max_dd_pct": round(mdd * 100, 1),
            "mean_r_taken": round(float(np.mean(r_taken)), 3) if r_taken else None}


def windows(t: pd.DataFrame, months: int, **kw) -> dict:
    t0, t1 = int(t.entry_t.min()), int(t.entry_t.max())
    res = []
    s = t0
    while s + months * MONTH_MS <= t1:
        g = t[(t.entry_t >= s) & (t.entry_t < s + months * MONTH_MS)]
        res.append(replay(g, **kw))
        s += MONTH_MS
    if not res:
        return {}
    xs = [r["x"] for r in res]
    return {"windows": len(res), "median_x": round(float(np.median(xs)), 2), "worst_x": round(min(xs), 2),
            "best_x": round(max(xs), 2), "pct_below_start": round(100 * float(np.mean([x < 1 for x in xs])), 1),
            "pct_floor_hit": round(100 * float(np.mean([r["floor_hit"] for r in res])), 1),
            "median_trades": int(np.median([r["taken"] for r in res])),
            "median_skipped_too_small": int(np.median([r["skipped_too_small"] for r in res])),
            "median_max_dd_pct": float(np.median([r["max_dd_pct"] for r in res])),
            "worst_max_dd_pct": float(max(r["max_dd_pct"] for r in res))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/lab/delta_trial_backtest.json")
    a = ap.parse_args()
    t = pd.concat([load_run("robust_4h", "4h"), load_run("robust_8h", "8h")], ignore_index=True)
    t = t[t.cfg.str.split("|").str[2] == LIVE_EXIT]
    t = tag_regimes(t)
    t = t[t.btc_vol_rank <= 2 / 3].sort_values("entry_t").reset_index(drop=True)
    print(f"live-config trades after the volatility filter: {len(t)}, mean {t.r_net.mean():+.3f}R")
    out = {"trades": int(len(t)), "mean_r": round(float(t.r_net.mean()), 4), "runs": {}}
    last12 = t[t.entry_t >= t.entry_t.max() - 12 * MONTH_MS]
    for start in (500.0, 1000.0, 2000.0, 5000.0):
        for risk in (0.01, 0.02, 0.03):
            for mo in (3, 5):
                kw = dict(start_inr=start, risk_pct=risk, max_open=mo)
                k = f"Rs{int(start)}_risk{int(risk * 100)}%_open{mo}"
                out["runs"][k] = {"w3": windows(t, 3, **kw), "w12": windows(t, 12, **kw),
                                  "last_12m": replay(last12, **kw), "full": replay(t, **kw)}
                r = out["runs"][k]
                print(f"{k:24} 3m: med x{r['w3']['median_x']} worst x{r['w3']['worst_x']} "
                      f"trades {r['w3']['median_trades']} too-small {r['w3']['median_skipped_too_small']} | "
                      f"12m: med x{r['w12']['median_x']} worst x{r['w12']['worst_x']} below-start {r['w12']['pct_below_start']}% "
                      f"floor {r['w12']['pct_floor_hit']}% dd {r['w12']['median_max_dd_pct']}/{r['w12']['worst_max_dd_pct']}% | "
                      f"last 12m x{r['last_12m']['x']} ({r['last_12m']['taken']} trades)")
    by_coin = (t.groupby("symbol").r_net.agg(["count", "mean"]).round(3)).to_dict("index")
    out["by_coin"] = by_coin
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print("by coin:", by_coin)
    print("saved", a.out)


if __name__ == "__main__":
    main()
