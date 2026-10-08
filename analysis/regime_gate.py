"""
Market-regime check for swing signals: skip when markets are wild or the coin is already trending hard.

Why. Five years of mirror-paired backtests (scripts/mirror_analysis.py, scripts/regime_filter.py) showed the
swing strategies earn +0.56 R per trade when Bitcoin is calm and nothing when it is wild, where the trade and its
mirror both lose. Breakouts in coins already trending strongly (daily ADX > 30) also earn nothing. Skipping both
raised the backtest from +0.20 to +0.36 R per trade, every year. The same split held on 56 other strategies.

Both inputs use only CLOSED daily bars:
    btc_vol_rank  Bitcoin's 30-day volatility, ranked against its own previous 365 days (0 = calmest, 1 = wildest)
    coin_adx      the coin's 14-day ADX on daily bars

The backtest (scripts/regime_filter.py) calls vol_rank_series and adx_series from here, so live and backtest
compute the same numbers (tests/test_regime_gate.py).

Modes (settings.swing_regime_filter): "off", "shadow" (tag every signal, still trade it), "on" (skip).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from analysis.lab import features as F

VOL_RANK_MAX = 0.67
ADX_MAX = 30.0


def vol_rank_series(closes: np.ndarray) -> np.ndarray:
    """For each daily close: where the 30-day volatility of daily log returns sits within the previous 365 days."""
    vol = pd.Series(np.diff(np.log(np.asarray(closes, float)), prepend=np.nan)).rolling(30).std()
    return vol.rolling(365, min_periods=180).apply(lambda w: (w[:-1] < w[-1]).mean(), raw=True).to_numpy()


def adx_series(h, l, c) -> np.ndarray:
    return F.adx(np.asarray(h, float), np.asarray(l, float), np.asarray(c, float))[0]


@dataclass
class Verdict:
    btc_vol_rank: float | None
    coin_adx: float | None
    reasons: list = field(default_factory=list)

    @property
    def would_skip(self) -> bool:
        return bool(self.reasons)

    def tag(self) -> str:
        """One line stored with the signal, read back by parse_tag to score the shadow test."""
        v = "na" if self.btc_vol_rank is None else f"{self.btc_vol_rank:.2f}"
        a = "na" if self.coin_adx is None else f"{self.coin_adx:.1f}"
        return f"regime: vol_rank={v} adx={a} verdict={'skip(' + ','.join(self.reasons) + ')' if self.reasons else 'take'}"

    def as_dict(self) -> dict:
        return {"btc_vol_rank": self.btc_vol_rank, "coin_adx": self.coin_adx, "would_skip": self.would_skip,
                "reasons": list(self.reasons)}


_TAG = re.compile(r"regime: vol_rank=(\S+) adx=(\S+) verdict=(take|skip\(([^)]*)\))")


def parse_tag(text: str) -> dict | None:
    m = _TAG.search(text or "")
    if not m:
        return None
    num = lambda s: None if s == "na" else float(s)
    reasons = [r for r in (m.group(4) or "").split(",") if r]
    return {"btc_vol_rank": num(m.group(1)), "coin_adx": num(m.group(2)), "would_skip": bool(reasons), "reasons": reasons}


def judge(btc_closes, coin_h, coin_l, coin_c, vol_rank_max: float = VOL_RANK_MAX, adx_max: float = ADX_MAX) -> Verdict:
    """Verdict from closed daily bars (oldest first). Missing history gives None for that input, never a skip."""
    rank = None
    if btc_closes is not None and len(btc_closes) >= 211:
        r = vol_rank_series(btc_closes)[-1]
        rank = None if np.isnan(r) else float(r)
    adx = None
    if coin_c is not None and len(coin_c) >= 60:
        a = adx_series(coin_h, coin_l, coin_c)[-1]
        adx = None if np.isnan(a) else float(a)
    reasons = []
    if rank is not None and rank > vol_rank_max:
        reasons.append("wild_market")
    if adx_max and adx is not None and adx > adx_max:
        reasons.append("strong_trend")
    return Verdict(rank, adx, reasons)


def shadow_scoreboard(signals: list[dict], trades: list[dict], match_minutes: float = 30.0) -> dict:
    """Score the shadow test: closed swing trades split by the verdict their signal carried.

    signals: [{symbol, signal_type, direction, timestamp (datetime), indicators_summary}]
    trades:  [{symbol, signal_type, side, opened_at (datetime), r}]
    A trade is matched to the newest tagged signal for the same coin, strategy and side logged up to
    match_minutes before it opened. If the filter works, "skip" trades should average well below "take".
    """
    tagged = []
    for s in signals:
        v = parse_tag(s.get("indicators_summary", ""))
        if v is not None:
            tagged.append((s, v))
    groups = {"take": [], "skip": [], "unmatched": []}
    by_reason: dict = {}
    rows = []
    for t in trades:
        best = None
        for s, v in tagged:
            if (s["symbol"] == t["symbol"] and s["signal_type"] == t["signal_type"] and s["direction"] == t["side"]):
                dt = (t["opened_at"] - s["timestamp"]).total_seconds() / 60
                if -1 <= dt <= match_minutes and (best is None or s["timestamp"] > best[0]["timestamp"]):
                    best = (s, v)
        if best is None:
            groups["unmatched"].append(t["r"])
            continue
        v = best[1]
        groups["skip" if v["would_skip"] else "take"].append(t["r"])
        for r in v["reasons"]:
            by_reason.setdefault(r, []).append(t["r"])
        rows.append({**{k: t[k] for k in ("symbol", "signal_type", "side", "r")}, **v})

    def stats(x):
        return {"n": len(x), "wins": sum(1 for r in x if r > 0), "avg_r": (sum(x) / len(x)) if x else None,
                "total_r": sum(x)}

    return {"take": stats(groups["take"]), "skip": stats(groups["skip"]), "untagged": stats(groups["unmatched"]),
            "by_reason": {k: stats(v) for k, v in by_reason.items()}, "trades": rows[-30:],
            "expected": {"take_r": 0.36, "skip_r": -0.05,
                         "note": "backtest: kept trades +0.36 R each; skipped ones about zero or worse. Judge after 30+ of each."}}


ROUND_TRIP_COST_PCT = 0.17     # fees + GST + slippage of a round trip, as % of price (lab cost model, india_gst)


def virtual_outcome(direction: str, entry: float, stop: float, target: float, t_ms, high, low, close,
                    start_ms: int, hold_ms: int, now_ms: int) -> tuple[str, float] | None:
    """Replay a signal on bars (oldest first, bar OPEN times) as if it had been traded: first touch of stop or
    target, the stop winning a same-bar tie (the backtest's rule), else closed at the end of the hold.
    Returns (outcome, R after costs), or None while it is still running."""
    sgn = 1 if direction == "long" else -1
    risk = abs(entry - stop) / entry * 100
    if risk <= 0:
        return None
    cost_r = ROUND_TRIP_COST_PCT / risk
    for t, h, lo, c in zip(t_ms, high, low, close):
        if t < start_ms:                                     # R8: only bars that open after the signal; the bar
            continue                                         # containing it holds pre-signal highs and lows
        if t > start_ms + hold_ms:
            return "expired", sgn * (c - entry) / entry * 100 / risk - cost_r
        hit_stop = lo <= stop if sgn > 0 else h >= stop
        hit_tgt = h >= target if sgn > 0 else lo <= target
        if hit_stop:
            return "lost", -1.0 - cost_r
        if hit_tgt:
            return "won", abs(target - entry) / entry * 100 / risk - cost_r
    if now_ms >= start_ms + hold_ms and len(close):
        return "expired", sgn * (close[-1] - entry) / entry * 100 / risk - cost_r
    return None


def signal_scoreboard(signals: list[dict]) -> dict:
    """Every tagged swing signal by verdict, scored by its replayed outcome (resolved rows only).
    signals: [{indicators_summary, outcome, pnl_pct, current_price, stop_loss, skip_reason}]"""
    groups: dict = {"take": [], "skip": []}
    pending = {"take": 0, "skip": 0}
    for s in signals:
        v = parse_tag(s.get("indicators_summary", ""))
        if v is None:
            continue
        g = "skip" if v["would_skip"] else "take"
        if s.get("outcome") in (None, "", "pending"):
            pending[g] += 1
            continue
        risk = abs(s["current_price"] - s["stop_loss"]) / s["current_price"] * 100 if s.get("current_price") else 0
        if risk > 0:
            groups[g].append(s["pnl_pct"] / risk)                # pnl_pct already includes costs (see resolver)
    st = lambda x: {"n": len(x), "wins": sum(1 for r in x if r > 0), "avg_r": (sum(x) / len(x)) if x else None,
                    "total_r": sum(x)}
    return {"take": {**st(groups["take"]), "running": pending["take"]},
            "skip": {**st(groups["skip"]), "running": pending["skip"]}}
