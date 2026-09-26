from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from analysis.crypto_signal import CryptoSignal
from analysis.crypto_signals import (
    BollingerSqueezeAnalyzer,
    ConfluenceAnalyzer,
    RSIDivergenceAnalyzer,
    SentimentShiftAnalyzer,
    VolumeSpikeAnalyzer,
)
from analysis.crypto_state import CryptoState
from config.settings import settings
from scheduler import pipeline

log = structlog.get_logger()


class CryptoEngine:
    """Orchestrates all crypto signal detectors with cooldown deduplication and thresholding."""

    def __init__(self) -> None:
        # Listed first because it is the one that requires agreement; the
        # others each fire on a single observation.
        self.confluence = ConfluenceAnalyzer()
        self.rsi_divergence = RSIDivergenceAnalyzer()
        self.volume_spike = VolumeSpikeAnalyzer()
        self.bollinger_squeeze = BollingerSqueezeAnalyzer()
        self.sentiment_shift = SentimentShiftAnalyzer()

        # Cooldown map: (symbol, signal_type) -> last_fired_utc
        self._cooldowns: dict[tuple[str, str], datetime] = {}
        self._recent_signals: list[CryptoSignal] = []
        # Last signal per (symbol, direction) that has neither hit its target
        # nor its stop. A second signal while the first is still live is the
        # same trade at a slightly later price, not a new idea.
        self._live: dict[tuple[str, str], CryptoSignal] = {}

    def process(self, state: CryptoState) -> list[CryptoSignal]:
        """Evaluate all signal strategies against current crypto market state."""
        if getattr(settings, "orderflow_enabled", True) and state.candles_1m:
            from analysis.orderflow import compute_cvd_trend
            state.cvd_trend = compute_cvd_trend(state.candles_1m)

        vol_candidate = self.volume_spike.analyze(state) if settings.crypto_volume_spike_enabled else None
        candidates: list[CryptoSignal | None] = [
            self.confluence.analyze(state),
            self.rsi_divergence.analyze(state),
            vol_candidate,
            self.bollinger_squeeze.analyze(state),
            self.sentiment_shift.analyze(state),
        ]

        fired: list[CryptoSignal] = []
        now = datetime.now(UTC)

        for sig in candidates:
            if sig is None:
                continue

            # Orderflow / CVD confirmation and absorption checks
            if getattr(settings, "orderflow_enabled", True) and state.candles_1m:
                from analysis.orderflow import cvd_alignment, detect_absorption
                absorption = detect_absorption(state.candles_1m)
                if absorption == "bullish_absorption":
                    if sig.direction == "short":
                        log.info("crypto_signal_vetoed_by_bullish_absorption", symbol=sig.symbol)
                        continue
                    elif sig.direction == "long":
                        sig.confidence = min(0.95, sig.confidence + 0.03)
                elif absorption == "bearish_absorption":
                    if sig.direction == "long":
                        log.info("crypto_signal_vetoed_by_bearish_absorption", symbol=sig.symbol)
                        continue
                    elif sig.direction == "short":
                        sig.confidence = min(0.95, sig.confidence + 0.03)

                aligns, net_delta, reason = cvd_alignment(state.candles_1m, sig.direction, window=15)
                if not aligns and abs(net_delta) > 0:
                    sig.confidence = max(0.50, sig.confidence - 0.05)
                    log.info("crypto_signal_cvd_divergence_penalty", symbol=sig.symbol, delta=net_delta, reason=reason)

            if sig.confidence < settings.crypto_min_confidence:
                log.debug("crypto_signal_below_threshold", symbol=sig.symbol, confidence=sig.confidence)
                pipeline.no_setup("setup found, confidence below the minimum", sig.signal_type)
                continue

            if self._opposes_htf_trend(sig, state):
                log.info("crypto_signal_opposes_htf_trend", symbol=sig.symbol,
                         direction=sig.direction, type=sig.signal_type)
                continue

            if self._is_on_cooldown(sig.symbol, sig.signal_type):
                log.debug("crypto_signal_on_cooldown", symbol=sig.symbol, type=sig.signal_type)
                pipeline.no_setup("setup found, same coin fired recently", sig.signal_type)
                continue

            if self._still_live(sig, state.current_price):
                log.debug("crypto_signal_duplicate_of_live", symbol=sig.symbol,
                          direction=sig.direction)
                pipeline.no_setup("setup found, same trade already live", sig.signal_type)
                continue

            opposing = self._opposing_live(sig, state.current_price)
            if opposing is not None:
                log.info("crypto_signal_contradicts_live", symbol=sig.symbol,
                          rejected=sig.direction, rejected_by=sig.signal_type,
                          live=opposing.direction, live_from=opposing.signal_type)
                continue

            self._set_cooldown(sig.symbol, sig.signal_type, now)
            self._live[(sig.symbol, sig.direction)] = sig
            fired.append(sig)
            self._recent_signals.append(sig)
            if len(self._recent_signals) > 100:
                self._recent_signals = self._recent_signals[-100:]

        return fired

    def _opposes_htf_trend(self, sig: CryptoSignal, state: CryptoState) -> bool:
        """
        Veto signals fighting the prevailing 1-hour / 15-minute trend.

        RSI divergence is exempt because it specifically hunts pivot reversals.
        Confluence, breakout, and volume surges must align with the higher timeframe.
        """
        if not getattr(settings, "crypto_htf_filter_enabled", True):
            return False

        # Daily trend first: the strongest evidence and the rule the 21-25 Sep
        # book needed (every losing short fought a rising coin).
        if getattr(settings, "daily_trend_filter_enabled", True):
            from analysis.daily_trend import against_daily_trend
            if against_daily_trend(sig.direction, state.current_price,
                                   getattr(state, "daily_sma20", None)):
                log.info("crypto_signal_against_daily_trend", symbol=sig.symbol,
                         direction=sig.direction, price=state.current_price,
                         sma20=state.daily_sma20)
                return True
        if sig.signal_type == "rsi_divergence":
            return False

        # 360 one-minute bars give 24 fifteen-minute bars, enough for the 20-bar
        # EMA. The feed used to fetch 200 (13 bars), so this filter silently
        # never ran; now a skip is logged instead of passing unseen.
        candles = state.get_candles("1h")
        if len(candles) < 20:
            candles = state.get_candles("15m")
        if len(candles) < 20:
            log.debug("htf_filter_skipped_short_history", symbol=state.symbol,
                      bars=len(candles))
            return False

        closes = [c.close for c in candles if c.is_closed]
        if len(closes) < 20:
            return False

        from analysis import indicators as ind
        ema_20 = ind.ema(closes, 20)
        if ema_20 is None or ema_20 <= 0:
            return False

        price = state.current_price
        # If long, veto if price is materially below the HTF 20 EMA (downtrend knife-catch)
        if sig.direction == "long" and price < ema_20 * 0.995:
            return True
        # If short, veto if price is materially above the HTF 20 EMA (uptrend short)
        if sig.direction == "short" and price > ema_20 * 1.005:
            return True

        return False

    def _get_signal_ttl(self, sig: CryptoSignal) -> timedelta:
        """Derive time-to-live from signal timeframe so live tracking does not deadlock."""
        tf = (sig.timeframe or "").strip().lower()
        if tf.endswith("m"):
            try:
                mins = int(tf[:-1])
                return timedelta(minutes=max(30, int(mins * 1.5)))
            except ValueError:
                pass
        elif tf.endswith("h"):
            try:
                hrs = int(tf[:-1])
                return timedelta(hours=max(1, hrs))
            except ValueError:
                pass
        return timedelta(minutes=60)

    def _still_live(self, sig: CryptoSignal, price: float, now: datetime | None = None) -> bool:
        """
        Is a previous signal for this symbol and direction still running?

        A timer alone cannot tell a fresh setup from the same move re-detected:
        one strongly trending coin produced four "new" longs inside an hour,
        every one of them the same continuous move at a later price.

        However, holding indefinitely without TTL caused complete watchlist
        starvation after a few hours when price hovered between target and stop.
        We check price resolution first, then age out old signals past their TTL.
        """
        prev = self._live.get((sig.symbol, sig.direction))
        if prev is None or price <= 0:
            return False

        cur_now = now or datetime.now(UTC)
        ttl = self._get_signal_ttl(prev)
        if (cur_now - prev.timestamp) >= ttl:
            self._live.pop((sig.symbol, sig.direction), None)
            return False

        long_ = sig.direction == "long"
        resolved = (price >= prev.target_price or price <= prev.stop_loss) if long_ \
            else (price <= prev.target_price or price >= prev.stop_loss)
        if resolved:
            self._live.pop((sig.symbol, sig.direction), None)
            return False
        return True

    def _opposing_live(self, sig: CryptoSignal, price: float,
                       now: datetime | None = None) -> CryptoSignal | None:
        """
        A live call for this symbol pointing the other way, or None.

        Observed on the live feed: a confluence SHORT on ETH at 11:17 backed by
        three independent families, then a volume-spike LONG on the same coin
        at 11:24 — two open, contradictory views on one market, seven minutes
        apart. Acting on both means paying two round trips to hold nothing.

        Neither existing guard could see it. `_live` is keyed by
        (symbol, direction), so a running short does not look at longs at all;
        the cooldown is keyed by (symbol, signal_type), so one analyzer firing
        never quiets another. Both are duplicate suppressors. This is the
        contradiction check, and it is deliberately strict: while a call is
        open, the reversal that matters is its own stop being hit. That is the
        position saying it was wrong, and until it does, a second opinion in
        the other direction is not new information — it is churn.
        """
        other = "short" if sig.direction == "long" else "long"
        prev = self._live.get((sig.symbol, other))
        if prev is None:
            return None

        cur_now = now or datetime.now(UTC)
        if (cur_now - prev.timestamp) >= self._get_signal_ttl(prev):
            self._live.pop((sig.symbol, other), None)
            return None

        if price > 0:
            prev_long = prev.direction == "long"
            resolved = (price >= prev.target_price or price <= prev.stop_loss) \
                if prev_long else \
                (price <= prev.target_price or price >= prev.stop_loss)
            if resolved:
                self._live.pop((sig.symbol, other), None)
                return None
        return prev

    def forget(self, sig: CryptoSignal) -> None:
        """
        Undo process() for a signal the runner dropped before publishing.

        process() marks a signal live and starts its cooldown the moment it is
        produced. When the confidence floor or the AI review then drops it,
        that bookkeeping stayed: a signal nobody saw blocked the next real
        one on the same coin for the whole cooldown.
        """
        key = (sig.symbol, sig.direction)
        if self._live.get(key) is sig:
            self._live.pop(key, None)
        self._cooldowns.pop((sig.symbol.lower(), sig.signal_type), None)

    def _is_on_cooldown(self, symbol: str, signal_type: str) -> bool:
        key = (symbol.lower(), signal_type)
        last = self._cooldowns.get(key)
        if last is None:
            return False
        cooldown = timedelta(minutes=settings.crypto_signal_cooldown_minutes)
        return (datetime.now(UTC) - last) < cooldown

    def _set_cooldown(self, symbol: str, signal_type: str, timestamp: datetime) -> None:
        self._cooldowns[(symbol.lower(), signal_type)] = timestamp

    def get_recent_signals(self, hours: int = 24) -> list[CryptoSignal]:
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        return [s for s in self._recent_signals if s.timestamp >= cutoff]
