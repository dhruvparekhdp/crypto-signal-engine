"""
Stop, target and trailing stop for the market simulator (owner, 26 Oct 2026).

The simulator was written before the live swing book. It used its own 1R
(1.8×ATR, never under 1.2% of price), a hardcoded "+0.8%" breakeven that was
not 0.8R, and a runner that added ATR to the price as if ATR were already
in R. The stepped clock and the batch replay also disagreed with each other.

Both now share this module, and the numbers are the live book's
(analysis/swing_book.py, 12–13 Oct 2026):

    1R  = 3 × ATR, never under 0.3% of price   (STOP_ATR, MIN_STOP_PCT)
    stop = sl_r × 1R          (live default sl_r = 1)
    target = tp_r × 1R        (live default tp_r = 3)
    trail  = the live ladder, via swing_lock: 1R→break-even, 1.5R→+0.5R,
             2R→+1R, 2.5R→+1.5R, then +0.5R locked per extra 0.5R

The trail updates from the bar's close, and only for the next bar. A stop
that was already live at the open of this bar is what can be hit now — the
close cannot move the stop and then get stopped on the same bar.
A bar that trades through both the stop and the target counts as the stop.
"""
from __future__ import annotations

from analysis.swing_book import MIN_STOP_PCT, STOP_ATR, swing_lock

# Live book defaults. The page can still override tp_r / sl_r / trail.
LIVE_TP_R = 3.0
LIVE_SL_R = 1.0


def one_r(entry: float, atr: float) -> float:
    """Price distance of 1R: 3×ATR, never tighter than 0.3% of price."""
    return max(STOP_ATR * max(float(atr), 0.0), MIN_STOP_PCT * abs(float(entry)))


def bracket(entry: float, atr: float, side: int, tp_r: float, sl_r: float) -> tuple[float, float, float]:
    """(stop, target, one_r distance). side +1 long, -1 short."""
    unit = one_r(entry, atr)
    stop = entry - side * sl_r * unit
    target = entry + side * tp_r * unit
    return stop, target, unit


def manage_bar(trade, high: float, low: float, close: float, *, trail: bool) -> str | None:
    """
    One bar of an open trade.

    Returns "stop", "target", or None. On an exit, sets exit_price, pnl_r
    (versus the *initial* stop, so a trailed lock reports the R it actually
    locked), pnl_pct and exit_reason. Does not set exit_time — the caller
    has the bar's timestamp.
    """
    side = 1 if str(trade.direction).upper() == "LONG" else -1
    entry = float(trade.entry_price)
    init = float(trade.init_sl_price or trade.sl_price)
    risk = abs(entry - init)
    if risk <= 0 or entry <= 0:
        return None

    # Excursion from this bar, against the original 1R.
    if side > 0:
        gain_pct = (high - entry) / entry * 100.0
        dd_pct = (low - entry) / entry * 100.0
    else:
        gain_pct = (entry - low) / entry * 100.0
        dd_pct = (entry - high) / entry * 100.0
    if gain_pct > trade.peak_gain_pct:
        trade.peak_gain_pct = gain_pct
        trade.peak_r = round(gain_pct / (risk / entry * 100.0), 2)
    if dd_pct < trade.max_drawdown_pct:
        trade.max_drawdown_pct = dd_pct
    target_dist = abs(float(trade.tp_price) - entry)
    if target_dist > 0:
        trade.target_pct_reached = min(100.0, max(0.0, trade.peak_gain_pct / (target_dist / entry * 100.0) * 100.0))

    stop = float(trade.sl_price)
    target = float(trade.tp_price)
    hit_sl = low <= stop if side > 0 else high >= stop
    hit_tp = high >= target if side > 0 else low <= target
    # Both in one bar: the stop was there first. Same rule the lab uses.
    if hit_sl:
        _fill(trade, stop, side, entry, risk, trailed=stop != init)
        return "stop"
    if hit_tp:
        _fill(trade, target, side, entry, risk, trailed=False)
        trade.exit_reason = "TAKE_PROFIT"
        return "target"

    if trail:
        new_stop, best = swing_lock(
            side, entry, init, stop, float(trade.peak_price or 0.0), float(close),
            mode="ladder", after_r=1.0, trail_atr=0.0,
        )
        trade.sl_price = new_stop
        trade.peak_price = best
        if abs(new_stop - init) > max(abs(entry) * 1e-8, 1e-9):
            trade.breakeven_ratchet_hit = True
    return None


def _fill(trade, price: float, side: int, entry: float, risk: float, *, trailed: bool) -> None:
    trade.exit_price = price
    trade.pnl_r = round(side * (price - entry) / risk, 3)
    trade.pnl_pct = round(side * (price - entry) / entry * 100.0, 3)
    if trade.pnl_r > 0.05:
        trade.exit_reason = "TRAIL_STOP" if trailed else "TAKE_PROFIT"
    elif trade.pnl_r > -0.05:
        trade.exit_reason = "BREAKEVEN_STOP"
    else:
        trade.exit_reason = "STOP_LOSS"
