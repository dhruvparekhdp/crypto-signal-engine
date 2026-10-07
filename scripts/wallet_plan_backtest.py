"""Detailed backtest of the 7 Oct owner plan: Rs5,000 paper wallet, withdraw Rs2,500 each time it reaches Rs10,000,
risk between 1% and 3%, margin cap 15%, Binance order rules, side risk cap 9%, all 8 live specs (4h + 8h).

    python -m scripts.wallet_plan_backtest --out data/lab/wallet_plan_backtest.json

Windows start every month and overlap: read the rates as "what if I had started then". In-sample: the
strategies were chosen on this history, so live results will be lower.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics

import numpy as np
import pandas as pd

from analysis.lab.wallet import WalletConfig, run_wallet
from scripts.sizing_backtest import load

USDT_INR = 102.0
START, SWEEP_AT, SWEEP = 5000 / USDT_INR, 10000 / USDT_INR, 2500 / USDT_INR
MONTH_MS = 30 * 86_400_000
WEAK = {"4h@ichimoku": 0.5, "8h@ichimoku": 0.5}        # null test 7 Oct: inconclusive at N=579
BASE = dict(start=START, target=1e15, bust_below=START * 0.05, live_sizing=True, leverage=10.0,
            max_concurrent=10_000, max_open_risk=0.09, reset=False, max_margin_frac=0.15, binance_rules=True,
            sweep_at=SWEEP_AT, sweep_amount=SWEEP)
VARIANTS = {
    "A fixed 3%": dict(risk_pct=0.03),
    "B fixed 2%": dict(risk_pct=0.02),
    "C fixed 1%": dict(risk_pct=0.01),
    "D 1-3% drawdown/streak": dict(risk_pct=0.03, risk_mode="adaptive", risk_min=0.01),
    "E 1-3% + evidence": dict(risk_pct=0.03, risk_mode="adaptive", risk_min=0.01, evidence=WEAK),
    "F E + Phase 1 guards": dict(risk_pct=0.03, risk_mode="adaptive", risk_min=0.01, evidence=WEAK,
                                 daily_loss_pct=0.09, loss_streak_pause=3, pause_hours=2.0),
    "G 3% no sweep (compare)": dict(risk_pct=0.03, sweep_at=None),
}


def one(win: pd.DataFrame, kw: dict) -> dict:
    cfg = WalletConfig(**{**BASE, **kw})
    r = run_wallet(win, cfg)
    c = r.cycles[0]
    return {"end": c["end"], "withdrawn": r.withdrawn, "sweeps": len(r.withdrawals), "dd": c["dd"],
            "bust": c["status"] == "BUST", "trades": c["trades"], "curve": r.curve, "w": r.withdrawals}


def windows(tr: pd.DataFrame, months: int, kw: dict) -> dict:
    t, t_last, rows = int(tr.entry_t.min()), int(tr.entry_t.max()), []
    while t + months * MONTH_MS <= t_last:
        win = tr[(tr.entry_t >= t) & (tr.entry_t < t + months * MONTH_MS)]
        rows.append(one(win, kw))
        t += MONTH_MS
    tot = [(x["end"] + x["withdrawn"]) / START for x in rows]
    return {"windows": len(rows),
            "median_total_x": round(statistics.median(tot), 2), "worst_total_x": round(min(tot), 2),
            "median_withdrawn_inr": round(statistics.median(x["withdrawn"] for x in rows) * USDT_INR),
            "median_end_inr": round(statistics.median(x["end"] for x in rows) * USDT_INR),
            "pct_with_a_withdrawal": round(100 * np.mean([x["sweeps"] > 0 for x in rows])),
            "pct_below_start": round(100 * np.mean([t_ < 1 for t_ in tot])),
            "pct_bust": round(100 * np.mean([x["bust"] for x in rows])),
            "median_max_dd_pct": round(100 * statistics.median(x["dd"] for x in rows)),
            "worst_max_dd_pct": round(100 * max(x["dd"] for x in rows)),
            "median_trades": round(statistics.median(x["trades"] for x in rows))}


def full_run(tr: pd.DataFrame, kw: dict) -> dict:
    x = one(tr, kw)
    by_year = {}
    for t, amt, _ in x["w"]:
        y = pd.to_datetime(t, unit="ms").year
        by_year[y] = by_year.get(y, 0) + round(amt * USDT_INR)
    eq = pd.Series({pd.to_datetime(t, unit="ms"): b for t, b in x["curve"]})
    year_end = {int(k.year): round(v * USDT_INR) for k, v in eq.resample("YE").last().items()} if len(eq) else {}
    return {"withdrawn_inr": round(x["withdrawn"] * USDT_INR), "sweeps": x["sweeps"], "end_inr": round(x["end"] * USDT_INR),
            "max_dd_pct": round(100 * x["dd"]), "trades": x["trades"], "withdrawn_by_year": by_year,
            "balance_at_year_end": year_end}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    tr = load()
    print(f"{len(tr)} trades {pd.to_datetime(tr.entry_t.min(), unit='ms'):%Y-%m} to "
          f"{pd.to_datetime(tr.entry_t.max(), unit='ms'):%Y-%m}; start Rs5,000, withdraw Rs2,500 at Rs10,000\n")
    out = {}
    for name, kw in VARIANTS.items():
        res = {"12m": windows(tr, 12, kw), "24m": windows(tr, 24, kw), "full": full_run(tr, kw)}
        out[name] = res
        w12, w24, f = res["12m"], res["24m"], res["full"]
        print(f"{name:26} 12m: x{w12['median_total_x']:<5} (worst x{w12['worst_total_x']:<4}) withdrew Rs{w12['median_withdrawn_inr']:>6,} "
              f"dd {w12['median_max_dd_pct']:>2}%/{w12['worst_max_dd_pct']:>2}% bust {w12['pct_bust']}% | "
              f"24m: x{w24['median_total_x']:<5} withdrew Rs{w24['median_withdrawn_inr']:>7,} | "
              f"5y: withdrew Rs{f['withdrawn_inr']:>8,} end Rs{f['end_inr']:>7,} dd {f['max_dd_pct']}%")
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(out, fh, indent=1, default=str)
        print("\nsaved", a.out)


if __name__ == "__main__":
    main()
