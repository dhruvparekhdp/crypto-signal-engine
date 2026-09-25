"""
Backtest the v2 setups the way they would really trade, and grade them.

Execution (research section 7.2, conservative on purpose):
  * entry is a post-only LIMIT at the trigger close. It fills only if a later
    5m bar trades THROUGH it (low < limit for a long), within `entry_bars`
    bars; otherwise the order is cancelled and nothing is booked. Maker fee.
  * stop is a market order: taker fee plus `stop_slip` adverse slippage.
  * target is a limit: maker fee, no slippage.
  * a bar that touches both stop and target counts as the stop.
  * time stop: after `time_stop_bars` 5m bars, if the trade is not at least
    +0.5R, it is closed at that bar's close (taker).
  * funding is charged on notional for the hours held, always as a cost.
  * one position per symbol at a time; candidates during a trade are skipped.

Every result is in R (multiples of the entry-to-stop risk) after all costs,
so leverage and wallet size do not enter into it.

Grading (the promotion gates to paper trading):
  >= 200 trades, expectancy >= +0.15R, profit factor >= 1.3, and positive in
  >= 60% of walk-forward windows. The rules have no fitted parameters, so
  every window is out-of-sample; if parameters are ever tuned, tune on one
  window and report only the next.
Monte Carlo: the R sequence resampled 10,000 times, for the 5th-percentile
outcome and the 95th-percentile drawdown at a given risk per trade.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from analysis.v2_setups import Candidate


@dataclass(frozen=True)
class ExecConfig:
    maker: float = 0.0002 * 1.18         # with GST
    taker: float = 0.0005 * 1.18
    stop_slip: float = 0.0005
    funding_per_8h: float = 0.0000655
    entry_bars: int = 3
    time_stop_bars: int = 24             # 2 hours of 5m bars
    time_stop_min_r: float = 0.5


@dataclass
class TradeResult:
    symbol: str
    setup: str
    side: str
    decided_at: str
    filled_at: str
    exit_at: str
    entry: float
    stop: float
    target: float
    exit: float
    reason: str          # target | stop | time
    r: float             # net of all costs
    bars: int


def simulate(cands: list[Candidate], k5: pd.DataFrame,
             ex: ExecConfig = ExecConfig()) -> list[TradeResult]:
    """Run candidates of ONE symbol against its 5m bars."""
    if not cands:
        return []
    k5 = k5.reset_index(drop=True)
    ts = k5["ts"].to_numpy()
    h, lo, c = (k5[x].to_numpy(dtype=float) for x in ("high", "low", "close"))
    close_times = ts + np.timedelta64(5, "m")
    busy_until = np.datetime64("1970-01-01")
    out: list[TradeResult] = []
    for cand in sorted(cands, key=lambda x: x.ts):
        decided = np.datetime64(cand.ts.to_datetime64())
        if decided < busy_until:
            continue
        start = int(np.searchsorted(ts, decided, side="left"))   # first bar opening at/after
        long = cand.side == "long"
        risk = abs(cand.entry - cand.stop)
        if risk <= 0:
            continue
        # 1. Does the resting limit fill?
        fill = None
        for b in range(start, min(start + ex.entry_bars, len(k5))):
            if (long and lo[b] < cand.entry) or (not long and h[b] > cand.entry):
                fill = b
                break
        if fill is None:
            continue
        # 2. Manage from the fill bar on.
        exit_px, reason, end = None, "", fill
        for b in range(fill, len(k5)):
            hit_stop = lo[b] <= cand.stop if long else h[b] >= cand.stop
            hit_target = h[b] >= cand.target if long else lo[b] <= cand.target
            # On the fill bar itself only the stop counts: the bar's order of
            # events is unknown, so a same-bar target is never assumed.
            if hit_stop:
                exit_px = cand.stop * (1 - ex.stop_slip if long else 1 + ex.stop_slip)
                reason, end = "stop", b
                break
            if hit_target and b > fill:
                exit_px, reason, end = cand.target, "target", b
                break
            if b - fill >= ex.time_stop_bars:
                r_now = ((c[b] - cand.entry) if long else (cand.entry - c[b])) / risk
                if r_now < ex.time_stop_min_r:
                    exit_px, reason, end = c[b], "time", b
                    break
        if exit_px is None:
            break                       # data ran out with the trade open
        move = (exit_px - cand.entry) / cand.entry * (1 if long else -1)
        hours = (end - fill + 1) * 5 / 60
        costs = ex.maker + (ex.maker if reason == "target" else ex.taker) \
            + ex.funding_per_8h * hours / 8
        r = (move - costs) / (risk / cand.entry)
        busy_until = close_times[end]
        out.append(TradeResult(cand.symbol, cand.setup, cand.side, str(cand.ts),
                               str(pd.Timestamp(ts[fill])), str(pd.Timestamp(close_times[end])),
                               cand.entry, cand.stop, cand.target, float(exit_px), reason,
                               round(float(r), 4), end - fill + 1))
    return out


# ── Statistics and gates ─────────────────────────────────────────────────────

def stats(rs: list[float]) -> dict:
    if not rs:
        return {"trades": 0}
    a = np.asarray(rs)
    wins, losses = a[a > 0], a[a <= 0]
    equity = np.cumsum(a)
    dd = float((np.maximum.accumulate(np.r_[0, equity])[1:] - equity).max())
    streak = cur = 0
    for x in a:
        cur = cur + 1 if x <= 0 else 0
        streak = max(streak, cur)
    return {
        "trades": int(len(a)),
        "win_rate": round(float(len(wins) / len(a)), 3),
        "expectancy_r": round(float(a.mean()), 3),
        "avg_win_r": round(float(wins.mean()), 3) if len(wins) else 0.0,
        "avg_loss_r": round(float(losses.mean()), 3) if len(losses) else 0.0,
        "profit_factor": (round(float(wins.sum() / -losses.sum()), 3)
                          if len(losses) and losses.sum() < 0 else None),
        "total_r": round(float(a.sum()), 2),
        "max_drawdown_r": round(dd, 2),
        "longest_losing_streak": int(streak),
    }


def walk_forward(trades: list[TradeResult], months: int = 2) -> list[dict]:
    """Results per consecutive window of `months`, by fill time."""
    if not trades:
        return []
    df = pd.DataFrame({"t": pd.to_datetime([t.filled_at for t in trades]),
                       "r": [t.r for t in trades]})
    df["w"] = (df["t"].dt.year * 12 + df["t"].dt.month - 1) // months
    out = []
    for _, g in df.groupby("w"):
        out.append({"window": str(g["t"].min().date()), "trades": len(g),
                    "total_r": round(float(g["r"].sum()), 2)})
    return out


def monte_carlo(rs: list[float], risk_pct: float = 0.5, runs: int = 10_000,
                seed: int = 1) -> dict:
    """Resample the trade sequence: 5th-percentile final equity and 95th-percentile drawdown."""
    if len(rs) < 10:
        return {}
    rng = np.random.default_rng(seed)
    a = np.asarray(rs)
    sims = rng.choice(a, size=(runs, len(a)), replace=True)
    growth = np.cumprod(1 + sims * risk_pct / 100.0, axis=1)
    peak = np.maximum.accumulate(growth, axis=1)
    dd = ((peak - growth) / peak).max(axis=1)
    return {
        "risk_pct_per_trade": risk_pct,
        "final_equity_p5": round(float(np.percentile(growth[:, -1], 5)), 3),
        "final_equity_p50": round(float(np.percentile(growth[:, -1], 50)), 3),
        "max_drawdown_p95_pct": round(float(np.percentile(dd, 95) * 100), 1),
    }


GATES = {"min_trades": 200, "min_expectancy_r": 0.15, "min_profit_factor": 1.3,
         "min_positive_windows": 0.60}


def grade(trades: list[TradeResult]) -> dict:
    """Stats, walk-forward, Monte Carlo and the pass/fail of every gate."""
    rs = [t.r for t in trades]
    s = stats(rs)
    wf = walk_forward(trades)
    pos = sum(1 for w in wf if w["total_r"] > 0) / len(wf) if wf else 0.0
    checks = {
        "trades": s.get("trades", 0) >= GATES["min_trades"],
        "expectancy": s.get("expectancy_r", -1) >= GATES["min_expectancy_r"],
        "profit_factor": (s.get("profit_factor") or 0) >= GATES["min_profit_factor"],
        "windows_positive": pos >= GATES["min_positive_windows"],
    }
    return {"stats": s, "positive_windows": round(pos, 2), "windows": wf,
            "monte_carlo": monte_carlo(rs), "gates": checks,
            "promote_to_paper": all(checks.values())}


def trades_to_rows(trades: list[TradeResult]) -> list[dict]:
    return [asdict(t) for t in trades]
