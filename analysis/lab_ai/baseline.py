"""A cheap non-AI entry filter to hold the LLM against: ridge regression on the same entry features,
trained only on trades that happened BEFORE the ones it filters (walk-forward, no peeking)."""
from __future__ import annotations

import numpy as np

KEYS = ["ret_1h", "ret_4h", "ret_24h", "rsi", "atr", "range_pos", "vol", "btc_24h", "ema50_dist", "stop_pct", "side"]


def _x(rows):
    return np.array([[r["features"][k] for k in KEYS] for r in rows], float)


def walk_forward(rows: list[dict], min_train: int = 120, block: int = 40, lam: float = 30.0) -> list[dict]:
    """Predict each block of trades from a ridge fit on everything earlier. Returns rows with `pred`."""
    rows = sorted(rows, key=lambda r: r["entry_t"])
    out = []
    for start in range(min_train, len(rows), block):
        train, test = rows[:start], rows[start:start + block]
        X, y = _x(train), np.array([r["r_net"] for r in train])
        mu, sd = X.mean(0), X.std(0) + 1e-9
        Z = np.c_[np.ones(len(X)), (X - mu) / sd]
        w = np.linalg.solve(Z.T @ Z + lam * np.eye(Z.shape[1]), Z.T @ y)
        pred = np.c_[np.ones(len(test)), (_x(test) - mu) / sd] @ w
        out += [{**r, "pred": float(p)} for r, p in zip(test, pred)]
    return out


def summarize(scored: list[dict]) -> dict:
    if not scored:
        return {"n": 0}
    r = np.array([x["r_net"] for x in scored])
    take = np.array([x["pred"] > 0 for x in scored])
    out = {"n": len(r), "all_exp_r": float(r.mean()), "all_total_r": float(r.sum()), "take_rate": float(take.mean())}
    if take.any():
        out.update(take_n=int(take.sum()), take_exp_r=float(r[take].mean()), take_total_r=float(r[take].sum()),
                   lift_r_per_trade=float(r[take].mean() - r.mean()))
    if (~take).any():
        out.update(skip_n=int((~take).sum()), skip_exp_r=float(r[~take].mean()))
    return out
