"""
Take the best deal first, and when a great one is open, stop.

The owner's rule (26 Sep): scan every coin, steady and volatile alike. If a
volatile coin offers a deal worth more than 50% on margin, take it and
treat the book as full; trade nothing else until it closes, then wait for
the next good move.

  deal_score      ranks signals that arrive on the same tick: confidence
                  times how many round trips of cost the target is worth
                  (capped), so a clean wide target beats a marginal one
  volatility      steady / normal / volatile, the coin's ATR relative to
                  the rest of the watchlist, shown in the trade log
  target_roe_pct  what the target pays on margin at the position's leverage
  is_premium      target_roe >= the premium threshold (50% by default)
  book_full       an open premium position fills the book
"""
from __future__ import annotations


def deal_score(signal, round_trip: float) -> float:
    """Higher is better. Confidence x (target distance / round-trip cost), capped at 10."""
    price = getattr(signal, "current_price", 0) or 0
    target = getattr(signal, "target_price", 0) or 0
    if price <= 0 or target <= 0 or round_trip <= 0:
        return 0.0
    reward = abs(target - price) / price
    return float(getattr(signal, "confidence", 0) or 0) * min(reward / round_trip, 10.0)


def volatility_classes(atr_pct_by_symbol: dict) -> dict:
    """
    'steady' / 'normal' / 'volatile' per coin, RELATIVE to the watchlist:
    the calmest third, the middle, the wildest third by ATR as % of price.
    Fixed thresholds do not travel: the engine's ATR is on 1m bars, where
    nearly every coin looks calm against daily-scale numbers.
    """
    known = sorted((v, k) for k, v in atr_pct_by_symbol.items() if v and v > 0)
    out = {k: "unknown" for k in atr_pct_by_symbol}
    n = len(known)
    for i, (_, sym) in enumerate(known):
        out[sym] = ("steady" if i < n / 3 else "volatile" if i >= 2 * n / 3 else "normal")
    return out


def target_roe_pct(pos) -> float:
    """Return on margin if the target fills: price distance x leverage, in %."""
    import math
    entry, target = pos.entry_price, pos.target_price
    if entry <= 0 or not target or not math.isfinite(target):
        return 0.0
    return abs(target - entry) / entry * pos.leverage * 100


def is_premium(pos, premium_roe_pct: float) -> bool:
    return premium_roe_pct > 0 and target_roe_pct(pos) >= premium_roe_pct


def book_full(open_positions: list, premium_roe_pct: float):
    """The open premium position that fills the book, or None."""
    for p in open_positions:
        if is_premium(p, premium_roe_pct):
            return p
    return None
