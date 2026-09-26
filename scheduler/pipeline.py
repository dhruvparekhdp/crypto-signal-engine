"""
Why are there no trades? Count every signal through every gate.

"No signal for a long time" had no answer without reading server logs: a
signal can die in the analysers, at the trend filters, at the confidence
floor, at the AI review, at the session window, the daily loss limit, the
losing-streak brake, the correlation cap, a full book, the fee floor... This
listens to the log events the engine already writes at each of those points
(a structlog processor, so no gate needs extra code) and keeps the last 24
hours in memory. GET /api/pipeline turns it into a funnel with reasons, and
the settings page shows it with the live state of each time-based gate.
"""
from __future__ import annotations

import time
from collections import Counter, deque

_events: deque = deque(maxlen=20000)          # (ts, stage, reason, symbol)
_last: dict[str, float] = {}

# Before a signal exists: why each analyser found no setup. These refusals are
# debug logs (thousands a day), which the log level drops before any processor
# sees them, so the analysers report here directly. One Counter per scan.
_scans: deque = deque(maxlen=1500)            # (ts, Counter of reasons, coins)
_scan_now: Counter = Counter()

# log event -> (stage, fixed reason or None to use the event's own `reason`)
EVENTS = {
    "crypto_signal_fired": ("fired", ""),
    "crypto_signal_below_min_confidence": ("blocked", "below minimum confidence"),
    "crypto_signal_dropped_after_ai_review": ("blocked", "AI review lowered confidence"),
    "crypto_signal_opposes_htf_trend": ("blocked", "against the 1h/15m trend"),
    "crypto_signal_against_daily_trend": ("blocked", "against the daily trend"),
    "crypto_signal_vetoed_by_bullish_absorption": ("blocked", "order-flow veto"),
    "crypto_signal_vetoed_by_bearish_absorption": ("blocked", "order-flow veto"),
    "crypto_signal_contradicts_live": ("blocked", "contradicts a live signal"),
    "paper_trades_blackout": ("paper_skipped", "news / economic-event pause"),
    "paper_trade_skipped": ("paper_skipped", None),
    "paper.rejected": ("paper_skipped", None),
    "paper_trade_opened": ("paper_opened", ""),
    "paper_trade_closed": ("paper_closed", ""),
}
REASON_WORDS = {
    "outside_session": "outside trading hours / weekend",
    "against_news_bias": "against the news bias",
    "daily_loss_limit": "daily loss limit reached",
    "losing_streak_pause": "3 losses in a row: 2-hour pause",
    "losing_streak_day_over": "5 losses in a row: done for the day",
    "pair_cooldown": "same coin closed < 15 min ago",
    "correlated_exposure": "already 2 trades in that direction",
    "book_full_premium": "premium trade open: book full",
    "max_concurrent": "max open trades reached",
    "already_open_in_symbol": "already a trade in that coin",
    "confidence_below_floor": "below paper min confidence",
    "no_free_margin": "no free margin",
    "stale": "signal went stale before the tick",
    "chased": "price already ran most of the way",
    "target_passed": "price already past the target",
    "stop_passed": "price already past the stop",
    "stop_inside_fees": "stop too close: inside fees",
    "liquidation_too_near": "liquidation too close to the stop",
    "below_one_lot": "position smaller than one lot",
    "target_not_viable": "target too small after fees",
}


def record(stage: str, reason: str = "", symbol: str = "") -> None:
    now = time.time()
    _events.append((now, stage, reason, symbol))
    _last[stage] = now


def no_setup(reason: str, analyser: str = "") -> None:
    """An analyser looked at a coin and found no tradeable setup, for `reason`."""
    _scan_now[f"{analyser.replace('_', ' ')}: {_plain(reason)}" if analyser
              else _plain(reason)] += 1


def end_scan(coins: int) -> None:
    """Close one analysis run (every coin checked once)."""
    _scans.append((time.time(), Counter(_scan_now), coins))
    _scan_now.clear()


def _plain(reason: str) -> str:
    """Group refusals that differ only in their numbers."""
    r = str(reason)
    if r.startswith("under 60 one-minute candles"):
        return "under 60 one-minute candles: history still loading"
    if r.startswith("could move"):
        return "market too quiet: the move cannot cover fees in time"
    if r.startswith("volatility in the bottom"):
        return "volatility at a low for this coin"
    if r.startswith("volume is"):
        return "thin volume: too few trades to carry a move"
    if r.startswith("bands are squeezed"):
        return "squeeze on: waiting for the breakout"
    if r.endswith("families disagree"):
        return "some checks point the other way"
    return r


def processor(logger, method_name, event_dict):
    """structlog processor: turn known engine events into pipeline records."""
    try:
        spec = EVENTS.get(event_dict.get("event"))
        if spec is not None:
            stage, reason = spec
            if reason is None:
                raw = str(event_dict.get("reason", "") or "")
                reason = REASON_WORDS.get(raw, raw.replace("_", " ") or "unspecified")
            record(stage, reason, str(event_dict.get("symbol", "") or "").upper())
    except Exception:
        pass
    return event_dict


def funnel(hours: float = 24.0) -> dict:
    """Counts per stage and per reason over the last `hours`, and when each stage last fired."""
    cutoff = time.time() - hours * 3600
    recent = [e for e in _events if e[0] >= cutoff]
    stages = Counter(e[1] for e in recent)
    reasons = {st: Counter(e[2] for e in recent if e[1] == st).most_common(12)
               for st in ("blocked", "paper_skipped")}
    scans = [x for x in _scans if x[0] >= cutoff]
    no_setup_total: Counter = Counter()
    for _, c, _coins in scans:
        no_setup_total.update(c)
    return {
        "hours": hours,
        "scans": len(scans),
        "coins": scans[-1][2] if scans else 0,
        "no_setup": [{"reason": r, "count": n} for r, n in no_setup_total.most_common(10)],
        "stages": dict(stages),
        "reasons": {k: [{"reason": r, "count": n} for r, n in v] for k, v in reasons.items()},
        "last": {k: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v)) for k, v in _last.items()},
        "tracking_since": (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_events[0][0]))
                           if _events else None),
    }
