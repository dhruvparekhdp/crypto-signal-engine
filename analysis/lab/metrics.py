"""Statistics on simulated trades. Everything is net of costs and expressed in R unless noted."""
from __future__ import annotations

import numpy as np
import pandas as pd

MONTH_MS = 30 * 86_400_000


def max_drawdown(r: np.ndarray) -> float:
    if not len(r):
        return 0.0
    c = np.cumsum(r)
    return float((np.maximum.accumulate(c) - c).max())


def trade_stats(df: pd.DataFrame, boot: int = 800, seed: int = 0) -> dict:
    n = len(df)
    if n == 0:
        return {"n": 0}
    r = df["r_net"].to_numpy(float)
    win = r > 0
    gw, gl = r[win].sum(), -r[~win].sum()
    rng = np.random.default_rng(seed)
    means = r[rng.integers(0, n, (boot, n))].mean(1) if n > 1 else np.array([r.mean()])
    lo, hi = np.percentile(means, [2.5, 97.5])
    sd = r.std(ddof=1) if n > 1 else 0.0
    span_days = max(1.0, (df["exit_t"].max() - df["entry_t"].min()) / 86_400_000)
    return {
        "n": n, "win_rate": float(win.mean()), "expectancy_r": float(r.mean()), "ci_lo": float(lo), "ci_hi": float(hi),
        "profit_factor": float(gw / gl) if gl > 0 else float("inf"), "avg_win_r": float(r[win].mean()) if win.any() else 0.0,
        "avg_loss_r": float(r[~win].mean()) if (~win).any() else 0.0,
        "t_stat": float(r.mean() / (sd / np.sqrt(n))) if sd > 0 else 0.0,
        "max_dd_r": max_drawdown(r), "total_r": float(r.sum()),
        "fee_drag_r": float((df["fee_frac"] / df["stop_frac"]).mean()),
        "trades_per_day": float(n / span_days), "avg_hold_min": float(df["bars"].mean()),
        "pct_stop": float((df["reason"] == "stop").mean()), "pct_target": float((df["reason"] == "target").mean()),
        "long_exp_r": float(df.loc[df.side > 0, "r_net"].mean()) if (df.side > 0).any() else None,
        "short_exp_r": float(df.loc[df.side < 0, "r_net"].mean()) if (df.side < 0).any() else None,
    }


def by(df: pd.DataFrame, col) -> pd.DataFrame:
    rows = []
    for k, g in df.groupby(col):
        s = trade_stats(g, boot=200)
        rows.append({"key": k, "n": s["n"], "win_rate": s["win_rate"], "expectancy_r": s["expectancy_r"],
                     "profit_factor": s["profit_factor"]})
    return pd.DataFrame(rows)


def walk_forward(trades: pd.DataFrame, cfg_col: str = "cfg", is_months: int = 6, oos_months: int = 2,
                 min_is_trades: int = 30) -> dict:
    """Pick the best config on each in-sample window by expectancy, then record what it
    did on the NEXT unseen window. Reports only the unseen results."""
    if trades.empty:
        return {"folds": []}
    t0, t1 = trades.entry_t.min(), trades.entry_t.max()
    folds, oos_all = [], []
    start = t0
    while start + (is_months + oos_months) * MONTH_MS <= t1 + MONTH_MS:
        a, b, c = start, start + is_months * MONTH_MS, start + (is_months + oos_months) * MONTH_MS
        ins = trades[(trades.entry_t >= a) & (trades.entry_t < b)]
        best, best_e = None, -1e9
        for cfg, g in ins.groupby(cfg_col):
            if len(g) >= min_is_trades and g.r_net.mean() > best_e:
                best, best_e = cfg, g.r_net.mean()
        if best is not None:
            oos = trades[(trades[cfg_col] == best) & (trades.entry_t >= b) & (trades.entry_t < c)]
            oos_all.append(oos)
            folds.append({"is_start": int(a), "oos_start": int(b), "cfg": best, "is_exp_r": float(best_e),
                          "oos_n": len(oos), "oos_exp_r": float(oos.r_net.mean()) if len(oos) else None})
        start += oos_months * MONTH_MS
    oos_df = pd.concat(oos_all) if oos_all else pd.DataFrame(columns=trades.columns)
    pos = [f for f in folds if f["oos_exp_r"] is not None and f["oos_exp_r"] > 0]
    return {"folds": folds, "oos": trade_stats(oos_df) if len(oos_df) else {"n": 0},
            "folds_positive": len(pos), "folds_total": len([f for f in folds if f["oos_n"]])}
