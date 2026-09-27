from __future__ import annotations

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
