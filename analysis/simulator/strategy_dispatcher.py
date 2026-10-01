"""
Modular Strategy Dispatcher for Market Simulator.

Allows testing individual quantitative strategies or the full ensemble:
- confluence: 5-family institutional confluence gate
- bollinger_squeeze: Volatility contraction followed by expansion breakout
- volume_spike: Volume anomaly spike & surge
- rsi_divergence: Regular & hidden RSI divergence
- trend_pullback: Trend structure pullback to EMA/VWAP & bounce
- range_breakout: Range compression break beyond ATR buffer
- sweep_reclaim: Key liquidity sweep of swing highs/lows with close reclaim
- breakout_retest: Swing level break & successful retest hold
- all: Multi-strategy ensemble (priority waterfall)
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from analysis import indicators as ind
from analysis.crypto_signals import (
    BollingerSqueezeAnalyzer,
    ConfluenceAnalyzer,
    RSIDivergenceAnalyzer,
    VolumeSpikeAnalyzer,
)
from analysis.crypto_state import CryptoState, OHLCVCandle
from analysis.patterns import range_breakout

log = logging.getLogger("simulator.strategy")

AVAILABLE_STRATEGIES = [
    {"id": "all", "name": "⚡ All Strategies (Ensemble / Multi-Strategy)", "desc": "Evaluates full confluence, bollinger, volume & structure"},
    {"id": "confluence", "name": "🎯 5-Family Confluence Gate", "desc": "Requires multi-family agreement across trend, mom, vol & structure"},
    {"id": "bollinger_squeeze", "name": "📊 Bollinger Bands Squeeze Breakout", "desc": "Volatility squeeze followed by directional expansion"},
    {"id": "volume_spike", "name": "📈 Volume Anomaly Spike & Surge", "desc": "Extreme volume anomaly relative to recent baseline"},
    {"id": "rsi_divergence", "name": "🔀 RSI Divergence (Trend Reversals)", "desc": "Price vs momentum discrepancies at cycle extremes"},
    {"id": "trend_pullback", "name": "🌊 Trend Pullback & Continuation", "desc": "Pullback into dynamic EMA support/resistance within macro trend"},
    {"id": "range_breakout", "name": "💥 Momentum Range Breakout", "desc": "Clean expansion breaking out of multi-bar consolidation"},
    {"id": "sweep_reclaim", "name": "🧹 Liquidity Sweep & Reclaim", "desc": "False breakout / liquidity run closing back inside structure"},
    {"id": "breakout_retest", "name": "🔁 Breakout & Retest Confirmation", "desc": "Prior resistance becomes support with confirmatory bounce"},
]


class SimulatorStrategyDispatcher:
    """Dispatches candle state to selected strategy or ensemble."""

    def __init__(self, selected_strategy: str = "all", **kwargs):
        strategy = kwargs.get("strategy_name") or kwargs.get("strategy") or selected_strategy or "all"
        self.selected_strategy = str(strategy).lower().strip()
        self.confluence_analyzer = ConfluenceAnalyzer()
        self.bollinger_analyzer = BollingerSqueezeAnalyzer()
        self.volume_analyzer = VolumeSpikeAnalyzer()
        self.rsi_analyzer = RSIDivergenceAnalyzer()

    def detect_signal(
        self,
        symbol: str,
        state: CryptoState,
        c_close: float,
        c_atr: float,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        i: int,
        rolling_candles: list[OHLCVCandle],
        htf_ema: float,
        is_htf_bull: bool,
        is_htf_bear: bool,
    ) -> tuple[int, str, float] | None:
        """
        Evaluate entry signal based on configured strategy.
        Returns: (target_dir [1 for LONG, -1 for SHORT], strategy_name, confidence) or None.
        """
        strat = self.selected_strategy

        # 1. 5-Family Confluence
        if strat in ("all", "confluence"):
            try:
                sig = self.confluence_analyzer.analyze(state)
                if sig and sig.confidence >= 0.60:
                    d = 1 if sig.direction.lower() == "long" else -1
                    return (d, sig.signal_type or "confluence", sig.confidence)
            except Exception:
                pass
            if strat == "confluence":
                return None

        # 2. Bollinger Squeeze Breakout
        if strat in ("all", "bollinger_squeeze", "bollinger"):
            try:
                sig = self.bollinger_analyzer.analyze(state)
                if sig and sig.confidence >= 0.65:
                    d = 1 if sig.direction.lower() == "long" else -1
                    return (d, "bollinger_squeeze", sig.confidence)
            except Exception:
                pass
            if strat in ("bollinger_squeeze", "bollinger"):
                return None

        # 3. Volume Spike & Surge
        if strat in ("all", "volume_spike", "volume"):
            try:
                sig = self.volume_analyzer.analyze(state)
                if sig and sig.confidence >= 0.65:
                    d = 1 if sig.direction.lower() == "long" else -1
                    return (d, "volume_spike", sig.confidence)
            except Exception:
                pass
            if strat in ("volume_spike", "volume"):
                return None

        # 4. RSI Divergence
        if strat in ("all", "rsi_divergence", "rsi"):
            try:
                # Direct divergence calculation on state candles (lookback 40)
                div = state.rsi_divergence(lookback=40)
                if div == "bullish":
                    return (1, "rsi_divergence", 0.70)
                elif div == "bearish":
                    return (-1, "rsi_divergence", 0.70)
            except Exception:
                pass
            if strat in ("rsi_divergence", "rsi"):
                return None

        # 5. Trend Pullback & Continuation (15m/5m Structure)
        if strat in ("all", "trend_pullback", "pullback"):
            if i >= 20:
                short_ema = float(pd.Series(closes[max(0, i - 14):i + 1]).ewm(span=10, adjust=False).mean().iloc[-1])
                # Long: in HTF bull, price pulls back to short EMA and closes strong
                if is_htf_bull and c_close >= short_ema and float(lows[i]) <= short_ema * 1.002 and c_close > float(opens[i]):
                    return (1, "trend_pullback", 0.68)
                # Short: in HTF bear, price pulls back to short EMA and closes weak
                if is_htf_bear and c_close <= short_ema and float(highs[i]) >= short_ema * 0.998 and c_close < float(opens[i]):
                    return (-1, "trend_pullback", 0.68)
            if strat in ("trend_pullback", "pullback"):
                return None

        # 6. Momentum Range Breakout
        if strat in ("all", "range_breakout", "breakout"):
            try:
                rb = range_breakout(rolling_candles, lookback=20, buffer_atr=c_atr * 0.25)
                if rb and rb.direction != 0:
                    d = 1 if rb.direction == 1 else -1
                    return (d, "range_breakout", 0.67)
            except Exception:
                pass
            if strat in ("range_breakout", "breakout"):
                return None

        # 7. Liquidity Sweep & Reclaim
        if strat in ("all", "sweep_reclaim", "liquidity_sweep"):
            if i >= 15:
                recent_low = float(np.min(lows[max(0, i - 15):i]))
                recent_high = float(np.max(highs[max(0, i - 15):i]))
                # Bullish sweep: Low dipped below recent_low but bar closed back above it
                if float(lows[i]) < recent_low and c_close > recent_low and c_close > float(opens[i]):
                    return (1, "sweep_reclaim", 0.72)
                # Bearish sweep: High pierced recent_high but bar closed back below it
                if float(highs[i]) > recent_high and c_close < recent_high and c_close < float(opens[i]):
                    return (-1, "sweep_reclaim", 0.72)
            if strat in ("sweep_reclaim", "liquidity_sweep"):
                return None

        # 8. Breakout & Retest Confirmation
        if strat in ("all", "breakout_retest", "retest"):
            if i >= 20:
                past_swing_high = float(np.max(highs[max(0, i - 20):i - 5]))
                past_swing_low = float(np.min(lows[max(0, i - 20):i - 5]))
                # Bullish retest: Price broke above past_swing_high previously, tested it as support (low <= level), and held
                if c_close > past_swing_high and float(lows[i]) <= past_swing_high * 1.003 and c_close > float(opens[i]):
                    return (1, "breakout_retest", 0.70)
                # Bearish retest: Price broke below past_swing_low, tested it as resistance, and held
                if c_close < past_swing_low and float(highs[i]) >= past_swing_low * 0.997 and c_close < float(opens[i]):
                    return (-1, "breakout_retest", 0.70)
            if strat in ("breakout_retest", "retest"):
                return None

        # Fallback for short warm-up in "all"
        if strat == "all" and len(rolling_candles) < 20 and i > 2:
            prev_h = float(highs[i - 1])
            prev_l = float(lows[i - 1])
            if c_close > prev_h and c_close > float(opens[i]):
                return (1, "momentum_break", 0.65)
            elif c_close < prev_l and c_close < float(opens[i]):
                return (-1, "momentum_break", 0.65)

        return None
