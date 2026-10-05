from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from config.settings import settings


@dataclass
class CryptoSignal:
    symbol: str                                                    # e.g. "btcusdt"
    signal_type: str                                               # "rsi_divergence" | "volume_spike" | "sentiment_shift" | "bollinger_squeeze" | "trend_continuation"
    direction: str                                                 # "long" | "short"
    trigger_description: str
    confidence: float                                              # 0.0 – 1.0
    current_price: float
    target_price: float | None
    stop_loss: float | None
    edge_pct: float                                                # Estimated theoretical expected edge %
    stake_pct: float                                               # Kelly-adjusted stake allocation
    timeframe: str                                                 # "30m", "1h", "4h", "1d"
    sentiment_score: float                                         # -1.0 to +1.0
    indicators_summary: str
    timestamp: datetime
    ai_review: str = ""
    # The crypto_signal_log row this signal was recorded as, so a later
    # decision (paper trade opened, or skipped and why) can be written back
    # onto the exact row the Signals page reads — set once, right after
    # logging, in scheduler/runner.py. 0 means "not logged yet".
    log_id: int = 0
    # "primary" (the signal a detector actually fired) or "mirror" (the
    # opposite-direction candidate synthesised alongside it when
    # settings.mirror_review_enabled is on). Never shown alone in the UI —
    # always paired with direction, e.g. "Primary (Long)" / "Mirror (Short)".
    candidate_role: str = "primary"
    # Set by analysis.crypto_engine.process() when a usable event-precedent
    # brief (analysis/event_precedent.py) is active and, if it has a real
    # directional lean, this signal's direction agrees with it. Read once,
    # at paper-trade open time, to decide whether to use the extended
    # hold-time ceiling instead of the normal one — never anything else.
    precedent_extended_hold: bool = False
    trade_mode: str = "intraday"         # "intraday" | "delivery"
    leverage_suggested: float = 10.0     # 5x-15x for intraday, 1x-3x for delivery
    tp1_price: float = 0.0               # First scale-out target (+1.0R)
    tp2_price: float = 0.0               # Second runner target (+2.0R to +2.5R)
    veto_reason: str = ""                # Explanation if rejected by filter
    regime: object = None                # analysis.regime_gate.Verdict for swing signals (shadow or live filter)


def compute_crypto_stake(edge_pct: float, confidence: float) -> float:
    """
    Quarter-Kelly criterion adjusted for cryptocurrency market volatility.
    Hard-capped at settings.crypto_max_stake_pct (default 2%).
    """
    if edge_pct <= 0 or confidence < 0.50:
        return 0.0

    # Kelly formula for binary/directional trade: f* = (p*b - q) / b
    # Here edge_pct approximates (p*b - q), scaled by confidence
    raw_kelly = (edge_pct / 100.0) * confidence
    quarter_kelly = raw_kelly * 0.25

    return round(min(max(quarter_kelly, 0.005), settings.crypto_max_stake_pct), 4)


def make_mirror_signal(sig: CryptoSignal) -> CryptoSignal:
    """
    Build the opposite-direction candidate for the mirror-review feature.

    Same symbol and entry price, direction flipped, target/stop distances
    mirrored from the original but each jittered independently (roughly
    +/- settings.mirror_target_jitter_pct) so the mirror is not a mechanical
    exact reflection of the original — similar magnitude, not identical
    numbers. Confidence is seeded at the ORIGINAL's pre-AI-review
    confidence: the mirror has not been through review yet, so it starts
    from the same place the primary started, not where the primary ended up.
    """
    jitter = max(0.0, float(getattr(settings, "mirror_target_jitter_pct", 0.20)))
    lo, hi = 1.0 - jitter, 1.0 + jitter

    entry = sig.current_price
    mirror_direction = "short" if sig.direction == "long" else "long"

    target_pct = abs((sig.target_price - entry) / entry) if (sig.target_price and entry) else 0.0
    stop_pct = abs((entry - sig.stop_loss) / entry) if (sig.stop_loss and entry) else 0.0

    m_target_pct = target_pct * random.uniform(lo, hi)
    m_stop_pct = stop_pct * random.uniform(lo, hi)

    if mirror_direction == "long":
        m_target = entry * (1 + m_target_pct)
        m_stop = entry * (1 - m_stop_pct)
    else:
        m_target = entry * (1 - m_target_pct)
        m_stop = entry * (1 + m_stop_pct)

    m_edge_pct = m_target_pct * 100.0
    m_stake_pct = compute_crypto_stake(m_edge_pct, sig.confidence)

    return CryptoSignal(
        symbol=sig.symbol,
        signal_type=sig.signal_type,
        direction=mirror_direction,
        trigger_description=f"Mirror of: {sig.trigger_description}",
        confidence=sig.confidence,
        current_price=entry,
        target_price=m_target,
        stop_loss=m_stop,
        edge_pct=round(m_edge_pct, 4),
        stake_pct=m_stake_pct,
        timeframe=sig.timeframe,
        sentiment_score=sig.sentiment_score,
        indicators_summary=sig.indicators_summary,
        timestamp=sig.timestamp,
        candidate_role="mirror",
    )
