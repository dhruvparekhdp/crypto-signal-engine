"""
Mirror review (27 Sep): tracker state and pure, no-AI helpers for the
confirmation-gated opening feature.

Kept separate from scheduler/runner.py because everything here is pure
computation over a CryptoSignal/CryptoState — no DB, no network, no
scheduler — which is what lets it be tested directly without spinning up
an AppRunner.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from analysis.crypto_signal import CryptoSignal
from analysis.crypto_state import CryptoState

# Minutes per recognised timeframe string. Falls back to parsing "Nm"/"Nh"/
# "Nd" for anything not in this table, and to 60 if that also fails —
# never a crash over a signal's own timeframe string.
_TIMEFRAME_MINUTES = {"30m": 30.0, "1h": 60.0, "4h": 240.0, "1d": 1440.0}


def timeframe_minutes(tf: str) -> float:
    key = (tf or "").strip().lower()
    if key in _TIMEFRAME_MINUTES:
        return _TIMEFRAME_MINUTES[key]
    try:
        if key.endswith("m"):
            return float(key[:-1])
        if key.endswith("h"):
            return float(key[:-1]) * 60.0
        if key.endswith("d"):
            return float(key[:-1]) * 1440.0
    except ValueError:
        pass
    return 60.0


def elapsed_pct(sig: CryptoSignal, now: datetime) -> float:
    ts = sig.timestamp
    if ts.tzinfo is None:
        from datetime import UTC
        ts = ts.replace(tzinfo=UTC)
    if now.tzinfo is None:
        from datetime import UTC
        now = now.replace(tzinfo=UTC)
    elapsed_min = (now - ts).total_seconds() / 60.0
    tf_min = timeframe_minutes(sig.timeframe)
    if tf_min <= 0:
        return 1.0
    return max(0.0, elapsed_min / tf_min)


@dataclass
class TrackedCandidate:
    """One candidate (primary or mirror) held by the mirror-review tracker
    between AI reviews, while its own market keeps moving."""

    signal: CryptoSignal
    log_id: int
    last_ai_review_at: datetime
    last_reviewed_confidence: float
    review_round: int = 0
    state: str = "tracking"          # "tracking" | "traded" | "rejected"
    rejection_reason: str = ""
    # This candidate's own round-0 crypto_signal_log id — stays fixed while
    # `log_id` moves to point at the latest round's row, so every re-review
    # row can carry parent_signal_id back to round 0 for get_review_trail().
    root_log_id: int = 0

    @property
    def calls_used(self) -> int:
        """1 for the round-0 review plus one per re-review already done."""
        return self.review_round + 1


@dataclass
class TrackedPair:
    """A fired signal's primary and (if any) mirror candidate, tracked
    together so the tie-break — higher confidence wins — can compare
    them in one place instead of two independent trackers racing."""

    primary: TrackedCandidate | None = None
    mirror: TrackedCandidate | None = None

    def candidates(self) -> list[TrackedCandidate]:
        return [c for c in (self.primary, self.mirror) if c is not None]

    def other(self, cand: TrackedCandidate) -> TrackedCandidate | None:
        if self.primary is cand:
            return self.mirror
        if self.mirror is cand:
            return self.primary
        return None

    def all_settled(self) -> bool:
        cands = self.candidates()
        return bool(cands) and all(c.state != "tracking" for c in cands)


def local_confidence_estimate(sig: CryptoSignal, state: CryptoState) -> float:
    """
    Cheap, local, no-AI re-score of a candidate's own direction against the
    current market state — reuses the same signals the detectors and the
    HTF trend filter already compute (EMA trend, CVD trend) plus how far
    price has actually travelled toward this candidate's own target/stop,
    rather than re-running a detector from scratch.

    No detector exposes a single "score this direction against current
    state" entry point of its own (each one both detects a setup AND
    scores it, in one entangled analyze() call) — this is the smallest
    stand-in that reuses real indicator computations instead of
    duplicating detector internals.
    """
    price = state.current_price
    if price <= 0:
        return sig.confidence

    conf = sig.confidence
    long_ = sig.direction == "long"

    # 1. Progress toward this candidate's own target/stop since it fired.
    if sig.target_price and sig.stop_loss and sig.current_price:
        total = abs(sig.target_price - sig.current_price)
        if total > 0:
            moved = (price - sig.current_price) if long_ else (sig.current_price - price)
            progress = moved / total
            conf += max(-0.06, min(0.06, progress * 0.10))

    # 2. Higher-timeframe EMA trend agreement — the same 20-EMA the HTF
    #    trend filter already computes in analysis/crypto_engine.py.
    try:
        from analysis import indicators as ind
        candles = state.get_candles("1h") or state.get_candles("15m")
        closes = [c.close for c in candles if c.is_closed]
        if len(closes) >= 20:
            ema_20 = ind.ema(closes, 20)
            if ema_20:
                if long_:
                    conf += 0.02 if price >= ema_20 else (
                        -0.03 if price < ema_20 * 0.995 else 0.0)
                else:
                    conf += 0.02 if price <= ema_20 else (
                        -0.03 if price > ema_20 * 1.005 else 0.0)
    except Exception:
        pass

    # 3. Order-flow (CVD) trend agreement.
    cvd = (getattr(state, "cvd_trend", "") or "").lower()
    if cvd:
        bullish = "bull" in cvd or "buy" in cvd
        bearish = "bear" in cvd or "sell" in cvd
        if bullish and not bearish:
            conf += 0.02 if long_ else -0.02
        elif bearish and not bullish:
            conf += 0.02 if not long_ else -0.02

    return round(max(0.30, min(0.95, conf)), 4)


def breach_reason(sig: CryptoSignal, price: float) -> str:
    """"mirror_stop_passed" / "mirror_target_passed" / "" — has price
    already crossed this candidate's own levels before it confirmed?"""
    if price <= 0 or not sig.target_price or not sig.stop_loss:
        return ""
    long_ = sig.direction == "long"
    if long_:
        if price <= sig.stop_loss:
            return "mirror_stop_passed"
        if price >= sig.target_price:
            return "mirror_target_passed"
    else:
        if price >= sig.stop_loss:
            return "mirror_stop_passed"
        if price <= sig.target_price:
            return "mirror_target_passed"
    return ""
