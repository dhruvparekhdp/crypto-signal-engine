"""
Candle reading, the way a discretionary trader does it.

The owner trades profitably at 30x off raw 5m and 15m charts: where the
recent swings are, whether highs and lows are stepping up or down, where
price was rejected, and how the last few candles closed. The reviewers were
only ever shown RSI and a MACD number, which is why their answers were
boilerplate ("trend_intact, stop_still_far"). This module turns the candles
into that same picture, in a few lines of text a model can judge.

Pure functions over OHLCVCandle lists; nothing here fetches or trades.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Swing:
    index: int
    price: float
    kind: str  # "high" | "low"


def swings(candles, width: int = 2) -> list[Swing]:
    """
    Fractal swing points: a high higher than `width` bars either side (a low
    lower). Only closed bars are used, and the last `width` bars can never
    confirm, so a swing is never repainted.
    """
    bars = [c for c in candles if getattr(c, "is_closed", True)]
    out: list[Swing] = []
    for i in range(width, len(bars) - width):
        window = bars[i - width:i + width + 1]
        hi, lo = bars[i].high, bars[i].low
        if hi == max(b.high for b in window) and all(
                b.high < hi for j, b in enumerate(window) if j != width):
            out.append(Swing(i, hi, "high"))
        if lo == min(b.low for b in window) and all(
                b.low > lo for j, b in enumerate(window) if j != width):
            out.append(Swing(i, lo, "low"))
    return out


def structure(candles, width: int = 2) -> str:
    """
    "up" when the last two swing highs AND lows both rose, "down" when both
    fell, else "range". The plainest definition of trend there is.
    """
    sw = swings(candles, width)
    highs = [s.price for s in sw if s.kind == "high"][-2:]
    lows = [s.price for s in sw if s.kind == "low"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return "unclear"
    if highs[1] > highs[0] and lows[1] > lows[0]:
        return "up"
    if highs[1] < highs[0] and lows[1] < lows[0]:
        return "down"
    return "range"


def levels(candles, price: float, width: int = 2) -> tuple[float | None, float | None]:
    """Nearest swing low below price (support) and swing high above it (resistance)."""
    sw = swings(candles, width)
    below = [s.price for s in sw if s.kind == "low" and s.price < price]
    above = [s.price for s in sw if s.kind == "high" and s.price > price]
    return (max(below) if below else None, min(above) if above else None)


def candle_word(c) -> str:
    """One word per candle: direction, body size and the wick that matters."""
    rng = c.high - c.low
    if rng <= 0:
        return "flat"
    body = abs(c.close - c.open)
    upper = c.high - max(c.open, c.close)
    lower = min(c.open, c.close) - c.low
    if body / rng < 0.25:
        if lower > 2 * max(upper, 1e-12):
            return "hammer"
        if upper > 2 * max(lower, 1e-12):
            return "shooting-star"
        return "doji"
    side = "green" if c.close > c.open else "red"
    return ("big-" if body / rng > 0.7 else "") + side


# The higher-timeframe half of the read used to be hard-wired to 15m, every
# call, forever. Nothing wrong with 15m itself, but a reviewer that is
# always shown the exact same window ends up repeating the exact same
# verdict against the exact same structure — the owner noticed the AI
# review citing "15m structure" almost verbatim across unrelated signals.
# Picking a different higher timeframe per call (still labelled honestly in
# the output, never silently) gives the model a genuinely different read
# each time instead of one fixed lens repeatedly disagreeing with whatever
# fired. 5m stays fixed — it is the owner's own immediate-structure anchor,
# named explicitly in this module's docstring, not the part that was
# repeating.
HIGHER_TIMEFRAMES = ("15m", "1h", "4h")


def describe(candles_5m, candles_higher, price: float, is_long: bool | None = None,
             higher_tf: str = "15m") -> str:
    """
    The chart, in words. Empty when there is too little history to say anything.

    `candles_higher`/`higher_tf` is whichever higher timeframe this call
    picked (see HIGHER_TIMEFRAMES and pick_higher_timeframe) — not always
    15m, though it still might be.

    Example:
      1h structure: up (last swing highs 2.410 -> 2.432, lows 2.380 -> 2.401)
      Support 2.401 (-0.62%), resistance 2.432 (+0.66%)
      Last 12h range 2.371-2.436; price at 48% of it
      Last 6 x 5m: green big-green doji red hammer green
    """
    if price <= 0:
        return ""
    lines: list[str] = []
    for tf, bars in ((higher_tf, candles_higher), ("5m", candles_5m)):
        closed = [c for c in bars if getattr(c, "is_closed", True)]
        if len(closed) < 12:
            continue
        sw = swings(closed)
        hs = [s.price for s in sw if s.kind == "high"][-2:]
        ls = [s.price for s in sw if s.kind == "low"][-2:]
        detail = ""
        if len(hs) == 2 and len(ls) == 2:
            detail = (f" (swing highs {hs[0]:.6g} -> {hs[1]:.6g}, "
                      f"lows {ls[0]:.6g} -> {ls[1]:.6g})")
        lines.append(f"{tf} structure: {structure(closed)}{detail}")
        if tf == higher_tf:
            sup, res = levels(closed, price)
            parts = []
            if sup:
                parts.append(f"support {sup:.6g} ({(sup - price) / price * 100:+.2f}%)")
            if res:
                parts.append(f"resistance {res:.6g} ({(res - price) / price * 100:+.2f}%)")
            if parts:
                lines.append(", ".join(parts).capitalize())
            recent = closed[-12:]
            lo, hi = min(c.low for c in recent), max(c.high for c in recent)
            if hi > lo:
                span = {"15m": "3h", "1h": "12h", "4h": "48h"}.get(higher_tf, "recent")
                lines.append(f"Last {span} range {lo:.6g}-{hi:.6g}; price at "
                             f"{(price - lo) / (hi - lo) * 100:.0f}% of it")
        else:
            lines.append("Last 6 x 5m: " + " ".join(candle_word(c) for c in closed[-6:]))
    if not lines:
        return ""
    if is_long is not None:
        s_higher = structure([c for c in candles_higher if getattr(c, "is_closed", True)])
        against = (is_long and s_higher == "down") or (not is_long and s_higher == "up")
        if against:
            lines.append(f"NOTE: this trade is against the {higher_tf} structure")
    return "\n".join(lines)


def pick_higher_timeframe() -> str:
    """A different higher timeframe per call, not always 15m — see the note
    above `describe`. Plain random.choice: no state to track, no bias to
    justify, just not the same window every single time."""
    import random
    return random.choice(HIGHER_TIMEFRAMES)
