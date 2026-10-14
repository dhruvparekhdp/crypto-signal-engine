"""
Basic signal rating (owner, 14 Oct 2026): a grade for every swing signal from the backtest data, recorded but NOT
used to trade yet. After a few days we compare grades with outcomes; only if A beats C does it earn a say.

Expected R per trade, live book (filter on, BCH/LTC out, 2021-2026, cluster-aware; docs/BACKTEST_REPORT.md):
by strategy spec and by coin, averaged, then nudged by the BTC volatility regime the signal fired in.
"""
from __future__ import annotations

SPEC_R = {"4h@donchian": 0.306, "4h@ichimoku": 0.296, "4h@keltner_break": 0.371, "4h@vol_breakout": 0.456,
          "8h@donchian": 0.375, "8h@ichimoku": 0.329, "8h@keltner_break": 0.421, "8h@vol_breakout": 0.473}
COIN_R = {"ADA": 0.474, "AVAX": 0.318, "BNB": 0.307, "BTC": 0.239, "DOGE": 0.474, "ETH": 0.296, "LINK": 0.558,
          "SOL": 0.588, "SUI": 0.476, "XRP": 0.230}
# unfiltered trades by BTC 30-day volatility rank: calm (<1/3) +0.36R, normal +0.40R, wild (>2/3) -0.32R
REGIME_R = {"calm": 0.362, "normal": 0.402, "wild": -0.323}
BOOK_R = 0.357
GRADES = (("A", 0.42), ("B", 0.32), ("C", -9.0))


def rate(signal_type: str, timeframe: str, symbol: str, btc_vol_rank: float | None) -> dict:
    spec = f"{timeframe}@{signal_type.removeprefix('swing_')}"
    coin = symbol.upper().removesuffix("USDT")
    parts = [SPEC_R.get(spec, BOOK_R), COIN_R.get(coin, BOOK_R)]
    base = sum(parts) / len(parts)
    regime = None
    if btc_vol_rank is not None:
        regime = "calm" if btc_vol_rank < 1 / 3 else "normal" if btc_vol_rank <= 2 / 3 else "wild"
        base *= REGIME_R[regime] / BOOK_R if REGIME_R[regime] > 0 else 0.0
        if REGIME_R[regime] <= 0:
            base = REGIME_R[regime]
    grade = next(g for g, cut in GRADES if base >= cut)
    return {"grade": grade, "expected_r": round(base, 3), "spec": spec, "coin": coin, "regime": regime}


def tag(r: dict) -> str:
    return f"rating={r['grade']}({r['expected_r']:+.2f}R)"
