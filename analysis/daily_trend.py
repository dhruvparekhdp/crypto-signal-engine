"""
Trade only with the daily trend.

The 21-25 Sep paper book lost Rs1,007, and all of it came from shorts: 26
shorts, none won, -Rs1,318, while the 16 longs made +Rs311. The engine kept
shorting coins that rose 6-26% over those days (XRP, LTC, BCH). The hourly
trend filter never ran, and even working it looks at hours, not days.

So: a long needs price above the 20-day average of daily closes, a short
needs price below it. No daily data yet means no opinion (the trade is not
blocked), and that is logged, so a missing feed cannot silently switch the
rule off for good.
"""
from __future__ import annotations


def sma(values: list[float], n: int = 20) -> float | None:
    vals = [v for v in values if v and v > 0]
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def against_daily_trend(direction: str, price: float, sma20: float | None) -> bool:
    """True when the trade fights the daily trend and should be refused."""
    if sma20 is None or sma20 <= 0 or price <= 0:
        return False
    if direction == "long":
        return price < sma20
    if direction == "short":
        return price > sma20
    return False
