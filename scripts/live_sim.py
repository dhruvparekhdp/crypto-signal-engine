"""Live simulator for the swing book (owner, 20 Oct 2026: "test all the strategy with live simulator ... show me result at
3% risk, 5% risk, and others ... check all the strategies one by one and also in all combination possible, how many
trade we won and lost"). Research only.

    python -m scripts.live_sim run [--out data/lab/live_sim.json]

Trades come from the lab (data/lab/runs/livesim_4h, livesim_8h): the 8 live specs on the 12 live coins (gold = PAXG,
Delta's XAUT is the same ounce), 1m execution, the live exit (3xATR stop, 3R target, 7 days, profit ladder 1/1.5/2/2.5R
with the 0.25R buffer and 1h-close confirmation = lab exit rb_std_c60_b25), Delta India fees (taker 0.05% + 18% GST each
side, 2 bps slippage, 3 bps more on stops) and funding. Then each signal goes through the live book's own rules, using
the live code (execution/delta_book.py):
  - BTC volatility filter (rank <= 0.67), BCH/LTC not traded
  - whole Delta contracts sized on the whole wallet (size_contracts, with the 5% round-up to one contract), leverage by
    stop distance (leverage_for, 5-10x; refused when liquidation would be within 1.2x of the stop), notional capped by
    the free margin x leverage x 0.9
  - at most 3 open, one per coin; signals on the same bar taken strongest first (swing_book.priority)
  - no new trades after -10% in a UTC day or below the floor (Rs250 on Rs500 = half the starting wallet)
Every combination of the 8 strategies (255), risk 1-10%, wallets Rs500-10,000, over the whole history, 2021-2024 (where
the strategies were chosen) and 2025-26 (after it). Drawdown is on closed trades.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from analysis import swing_book as sb
from analysis.instruments import DELTA_CONTRACT
from execution.delta_book import MIN_STOP_FRAC, leverage_for, liquidation_distance, size_contracts
from execution.delta_india import Product

USD_INR = 85.0
DAY = 86_400_000
SPECS = [("4h", "vol_breakout"), ("4h", "keltner_break"), ("4h", "donchian"), ("4h", "ichimoku"),
         ("8h", "keltner_break"), ("8h", "vol_breakout"), ("8h", "donchian"), ("8h", "ichimoku")]
LABEL = {("4h", "vol_breakout"): "4h vol breakout", ("4h", "keltner_break"): "4h keltner", ("4h", "donchian"): "4h donchian",
         ("4h", "ichimoku"): "4h ichimoku", ("8h", "keltner_break"): "8h keltner", ("8h", "vol_breakout"): "8h vol breakout",
         ("8h", "donchian"): "8h donchian", ("8h", "ichimoku"): "8h ichimoku"}
RISKS = (0.01, 0.02, 0.03, 0.04, 0.05, 0.075, 0.10)
WALLETS = (500, 1000, 2000, 5000, 10000)
PERIODS = {"all": (None, None), "2021-24": (None, "2025-01-01"), "2025-26": ("2025-01-01", None)}
GOLD = "PAXGUSDT"


def load_trades() -> pd.DataFrame:
    from scripts.edge_report import tag_regimes
    parts = []
    for tf in ("4h", "8h"):
        t = pd.read_parquet(Path(f"data/lab/runs/livesim_{tf}/trades.parquet"))
        t["tf"] = tf
        parts.append(t)
    t = pd.concat(parts, ignore_index=True)
    t["spec"] = [SPECS.index((tf, s)) if (tf, s) in SPECS else -1 for tf, s in zip(t.tf, t.strategy)]
    t = t[t.spec >= 0].copy()
    t["coin"] = np.where(t.symbol == GOLD, "XAUT", t.symbol.str.replace("USDT", "", regex=False))
    t = tag_regimes(t)
    t["filter_ok"] = t.btc_vol_rank <= 2 / 3
    t["prio"] = [sb.priority(f"swing_{s}", tf, c + "USDT") for s, tf, c in zip(t.strategy, t.tf, t.coin)]
    return t.sort_values(["entry_t", "prio"], ascending=[True, False]).reset_index(drop=True)


def _product(coin: str) -> Product:
    return Product(0, coin + "USD", DELTA_CONTRACT[coin], 0.0001, 1.0, coin)


def simulate(t: pd.DataFrame, start_inr: float, risk: float, round_up: float = 0.05, max_open: int = 3,
             daily_loss: float = 0.10, floor_frac: float = 0.5, keep_trades: bool = False) -> dict:
    wallet = float(start_inr)
    floor = start_inr * floor_frac
    peak, mdd = wallet, 0.0
    open_pos: list[dict] = []
    day, day_start = None, wallet
    skips = {"filter": 0, "full": 0, "coin_open": 0, "too_small": 0, "too_wide": 0, "daily_loss": 0, "floor": 0}
    closed: list[dict] = []
    busted = False
    products = {}

    def settle(upto):
        nonlocal wallet, peak, mdd
        for p in sorted([p for p in open_pos if p["exit_t"] <= upto], key=lambda p: p["exit_t"]):
            wallet += p["pnl"]
            open_pos.remove(p)
            peak = max(peak, wallet)
            mdd = max(mdd, 1 - wallet / peak if peak > 0 else 0.0)
            closed.append(p)

    for row in t.itertuples(index=False):
        settle(row.entry_t)
        if not row.filter_ok:
            skips["filter"] += 1
            continue
        d = row.entry_t // DAY
        if d != day:
            day, day_start = d, wallet
        if wallet < floor:
            busted = True
            skips["floor"] += 1
            continue
        if wallet < day_start * (1 - daily_loss):
            skips["daily_loss"] += 1
            continue
        if len(open_pos) >= max_open:
            skips["full"] += 1
            continue
        if any(p["coin"] == row.coin for p in open_pos):
            skips["coin_open"] += 1
            continue
        sf = float(row.stop_frac)
        lev = leverage_for(sf)
        if sf < MIN_STOP_FRAC or liquidation_distance(lev) < sf * 1.2:
            skips["too_wide"] += 1
            continue
        prod = products.setdefault(row.coin, _product(row.coin))
        stop = row.entry * (1 - row.side * sf)
        n, _, _ = size_contracts(prod, row.entry, stop, wallet, risk, USD_INR, max(round_up, risk))
        free = wallet - sum(p["margin"] for p in open_pos)
        cap = int((free / USD_INR) * lev * 0.9 // (prod.contract_value * row.entry)) if row.entry > 0 else 0
        n = min(n, cap)
        if n < 1:
            skips["too_small"] += 1
            continue
        notional = n * prod.contract_value * row.entry * USD_INR
        open_pos.append({"exit_t": int(row.exit_t), "entry_t": int(row.entry_t), "coin": row.coin, "spec": int(row.spec),
                         "pnl": notional * row.net_ret, "margin": notional / lev, "r": float(row.r_net),
                         "risk_inr": notional * sf, "contracts": n, "reason": row.reason, "side": int(row.side)})
    settle(float("inf"))
    pnl = np.array([p["pnl"] for p in closed])
    rs = np.array([p["r"] for p in closed])
    streak = cur = 0
    for x in pnl:
        cur = cur + 1 if x <= 0 else 0
        streak = max(streak, cur)
    t0, t1 = (int(t.entry_t.min()), int(t.entry_t.max())) if len(t) else (0, 1)
    years = max((t1 - t0) / (365.25 * DAY), 0.25)
    x = wallet / start_inr
    out = {"taken": len(closed), "won": int((pnl > 0).sum()), "lost": int((pnl <= 0).sum()),
           "win_pct": round(100 * float((pnl > 0).mean()), 1) if len(pnl) else None,
           "avg_r": round(float(rs.mean()), 3) if len(rs) else None, "total_r": round(float(rs.sum()), 1),
           "end_inr": round(wallet, 0), "x": round(x, 3),
           "cagr_pct": round(100 * (x ** (1 / years) - 1), 1) if x > 0 else -100.0,
           "max_dd_pct": round(100 * mdd, 1), "losing_streak": streak, "busted": busted,
           "per_month": round(len(closed) / (years * 12), 2), "skips": skips,
           "avg_risk_pct": round(100 * float(np.mean([p["risk_inr"] for p in closed])) / start_inr, 2) if closed else None}
    if keep_trades:
        out["trades"] = closed
    return out


_T: pd.DataFrame | None = None


def _init(t_path: str) -> None:
    global _T
    _T = pd.read_pickle(t_path)


def _job(args):
    combo, risk, wallet, period = args
    t = _T
    lo, hi = PERIODS[period]
    if lo:
        t = t[t.entry_t >= pd.Timestamp(lo, tz="UTC").value // 10**6]
    if hi:
        t = t[t.entry_t < pd.Timestamp(hi, tz="UTC").value // 10**6]
    t = t[t.spec.isin(combo)]
    r = simulate(t, wallet, risk)
    r.update(combo=list(combo), risk=risk, wallet=wallet, period=period)
    return r


def run(out_path: str) -> None:
    t = load_trades()
    tmp = Path("data/lab/live_sim_trades.pkl")
    t.to_pickle(tmp)
    combos = [c for k in range(1, 9) for c in itertools.combinations(range(8), k)]
    jobs = [(c, r, w, p) for c in combos for r in RISKS for w in WALLETS for p in PERIODS]
    print(f"{len(t)} signals ({int(t.filter_ok.sum())} pass the BTC filter), {len(combos)} combinations, {len(jobs)} runs",
          flush=True)
    rows = []
    prog = Path("data/lab/live_sim_progress.json")
    started = time.time()

    def progress(done: int, stage: str) -> None:
        prog.write_text(json.dumps({"stage": stage, "done": done, "total": len(jobs), "started": started,
                                    "updated": time.time(), "signals": int(len(t)),
                                    "signals_after_filter": int(t.filter_ok.sum()), "combos": len(combos)}))
    progress(0, "simulating")
    with ProcessPoolExecutor(max_workers=8, initializer=_init, initargs=(str(tmp),)) as ex:
        for i, r in enumerate(ex.map(_job, jobs, chunksize=64)):
            rows.append(r)
            if i % 250 == 0:
                progress(i, "simulating")
            if i % 5000 == 0:
                print(f"  {i}/{len(jobs)}", flush=True)
    progress(len(jobs), "writing results")
    # equity curves and trade lists for the configurations the page draws
    curves = {}
    for combo in [tuple(range(8))] + [(i,) for i in range(8)]:
        for risk in (0.03, 0.05):
            r = simulate(t[t.spec.isin(combo)], 500, risk, keep_trades=True)
            key = f"{','.join(map(str, combo))}|{risk}"
            eq, w = [], 500.0
            for p in sorted(r["trades"], key=lambda p: p["exit_t"]):
                w += p["pnl"]
                eq.append([p["exit_t"], round(w, 1)])
            curves[key] = {"equity": eq, "trades": [
                {"entry_t": p["entry_t"], "exit_t": p["exit_t"], "coin": p["coin"], "spec": p["spec"], "side": p["side"],
                 "contracts": p["contracts"], "r": round(p["r"], 3), "pnl": round(p["pnl"], 2), "reason": p["reason"]}
                for p in r["trades"]] if combo == tuple(range(8)) else []}
    per_spec = (t[t.filter_ok].groupby("spec").agg(signals=("r_net", "size"), win_pct=("r_net", lambda r: round(100 * (r > 0).mean(), 1)),
                                                   avg_r=("r_net", "mean")).round(3).reset_index().to_dict("records"))
    meta = {"labels": [LABEL[s] for s in SPECS], "risks": RISKS, "wallets": WALLETS, "periods": list(PERIODS),
            "data_from": str(pd.to_datetime(t.entry_t.min(), unit="ms").date()),
            "data_to": str(pd.to_datetime(t.entry_t.max(), unit="ms").date()),
            "signals": int(len(t)), "signals_after_filter": int(t.filter_ok.sum()), "per_spec_signals": per_spec,
            "coins": sorted(t.coin.unique().tolist())}
    Path(out_path).write_text(json.dumps({"meta": meta, "runs": rows, "curves": curves}, default=float))
    print("saved", out_path, len(rows), "runs")
    progress(len(jobs), "done")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["run"])
    ap.add_argument("--out", default="data/lab/live_sim.json")
    a = ap.parse_args()
    run(a.out)
