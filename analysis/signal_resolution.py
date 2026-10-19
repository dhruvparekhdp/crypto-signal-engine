"""
How a logged intraday signal is labelled won / lost / expired (owner, 19 Oct 2026: "fix the resolver labels").

A signal says "target within N minutes" (its timeframe, e.g. "81m", is levels.horizon_minutes - the expected time to
target). It is judged on exactly that claim, on the 1-minute bars that open after it fired:

  - the stop is checked before the target on every bar, so a bar that touches both is a LOSS. The old rule credited
    the target whenever that bar closed in the trade's favour, which a 1-minute bar cannot tell us;
  - the window is the signal's own horizon, not the paper engine's 24-hour hold. A replay of 1,687 signals
    (scripts/signal_replay.py) found 513 'won' labels of which only 334 reached the target inside the signal's own
    window - 146 got there hours later;
  - expiry is decided only once the whole window has been seen, at the close of its last bar. The old loop could
    expire a signal on the first bar it checked as soon as the newest candle was past the hold time;
  - a hole in the bars (the engine keeps 2-8 hours of 1m candles in memory, less after a restart) is reported as a
    gap rather than labelled from whatever bars happen to be left.

The pnl is the move from the logged price to the exit, in percent, positive when it went the signal's way (as before).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DEFAULT_WINDOW_MIN = 240
BAR = timedelta(minutes=1)
MAX_GAP = timedelta(minutes=2)          # first bar after the signal, and between bars


@dataclass(frozen=True)
class Resolution:
    status: str                         # won | lost | expired | running | gap
    pnl_pct: float = 0.0
    minutes: int | None = None


def window_minutes(timeframe: str | None, default: int = DEFAULT_WINDOW_MIN) -> int:
    """'81m' -> 81, '4h' -> 240, '1d' -> 1440; anything else -> default."""
    m = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", str(timeframe or "").lower())
    if not m:
        return default
    n = int(m.group(1)) * {"m": 1, "h": 60, "d": 1440}[m.group(2)]
    return n if n > 0 else default


def _naive(ts: datetime) -> datetime:
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


def _bar(b) -> tuple[datetime, float, float, float]:
    """(open time, high, low, close) from an OHLCVCandle or a store-shaped dict."""
    if isinstance(b, dict):
        return _naive(b["timestamp"]), float(b["high"]), float(b["low"]), float(b["close"])
    return _naive(b.timestamp), float(b.high), float(b.low), float(b.close)


def resolve(direction: str, price: float, target: float, stop: float, signal_ts: datetime, bars,
            window_min: int) -> Resolution:
    """Label one signal from 1-minute bars (open-time stamped, any order, closed bars only)."""
    long_ = direction == "long"
    t0 = _naive(signal_ts)
    end = t0 + timedelta(minutes=window_min)
    seq = sorted((x for x in map(_bar, bars) if x[0] > t0), key=lambda x: x[0])   # bars that open after the signal
    if not seq or seq[0][0] - t0 > MAX_GAP:
        return Resolution("gap")

    def pnl(px: float) -> float:
        return round((px - price) / price * 100 * (1 if long_ else -1), 4)

    prev = seq[0][0]
    last_close = None
    seen_past_end = False
    for ts, hi, lo, close in seq:
        if ts >= end:
            seen_past_end = True
            break
        if ts - prev > MAX_GAP:
            return Resolution("gap")
        prev = ts
        mins = int((ts - t0).total_seconds() // 60) + 1
        if (lo <= stop) if long_ else (hi >= stop):
            return Resolution("lost", pnl(stop), mins)
        if (hi >= target) if long_ else (lo <= target):
            return Resolution("won", pnl(target), mins)
        last_close = close
    if prev + BAR >= end and last_close is not None:
        return Resolution("expired", pnl(last_close), window_min)
    return Resolution("gap" if seen_past_end else "running")    # bars after the window but a hole before its end
