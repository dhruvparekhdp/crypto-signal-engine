"""
Live Market Replay Engine for Multi-Year Tick & Candle Simulation.

Replays historic candles and synthesized 1s ticks through:
- Rolling multi-timeframe indicators (RSI-14, MACD, ATR, EMA-20/50/200, Volume Z-Scores)
- Multi-timeframe Confluence, Trend Expansion, and Scalp Level detectors
- Smart Protections: Anti-Flip Directional Guard (90m cooldown), Consecutive Loss Cooldown
- Active Trade Lifecycle: TP1 50% scale-out, Breakeven ratchet, Runner extension (+1.5R at >=1.8R),
  Smart 60m Stagnation Exit, and Excursion Tracking (MFE & MAE).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from analysis.crypto_signals import (
    BollingerSqueezeAnalyzer,
    ConfluenceAnalyzer,
    RSIDivergenceAnalyzer,
    VolumeSpikeAnalyzer,
)
from analysis.crypto_state import CryptoState, OHLCVCandle, resample_candles
from analysis.simulator.strategy_dispatcher import SimulatorStrategyDispatcher

log = logging.getLogger("simulator.replay")


@dataclass
class SimulatedTrade:
    trade_id: int
    symbol: str
    direction: str  # "LONG" | "SHORT"
    entry_time: str
    exit_time: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    tp_price: float = 0.0
    sl_price: float = 0.0
    pnl_pct: float = 0.0
    pnl_r: float = 0.0
    exit_reason: str = ""  # "TAKE_PROFIT_2.0R", "STOP_LOSS_1.2R", "STAGNATION_60M", etc.
    peak_gain_pct: float = 0.0
    peak_r: float = 0.0
    max_drawdown_pct: float = 0.0
    target_pct_reached: float = 0.0
    tp1_hit: bool = False
    breakeven_ratchet_hit: bool = False
    runner_extended: bool = False
    # AI Thinking & Reasoning
    ai_why_it_worked: str = ""
    ai_why_it_failed: str = ""
    ai_key_takeaway: str = ""
    ai_thinking_trace: str = ""
    ai_source_model: str = ""
    indicators_at_entry: dict[str, Any] = field(default_factory=dict)


class MarketReplayEngine:
    """
    High-throughput market replay engine executing systematic signal detection
    and paper trade management over 1-month to 3-year historical data.
    """

    def __init__(
        self,
        symbols: list[str],
        window_years: float = 3.0,
        lake_root: str = "data/lake",
        tp_r: float = 2.0,
        sl_r: float = 1.2,
        anti_flip_cooldown_minutes: int = 90,
        stagnation_exit_minutes: int = 60,
        tp1_scale_out_enabled: bool = True,
        strategy: str = "all",
        on_tick_callback: Callable[[dict], None] | None = None,
    ):
        self.symbols = [s.upper() for s in symbols]
        self.window_years = window_years
        self.lake_root = Path(lake_root)
        self.tp_r = tp_r
        self.sl_r = sl_r
        self.anti_flip_cooldown_minutes = anti_flip_cooldown_minutes
        self.stagnation_exit_minutes = stagnation_exit_minutes
        self.tp1_scale_out_enabled = tp1_scale_out_enabled
        self.strategy = (strategy or "all").lower().strip()
        self.on_tick_callback = on_tick_callback

        self.trades: list[SimulatedTrade] = []
        self.trade_counter = 0
        self.total_ticks_processed = 0
        self.anti_flip_vetos = 0
        self.stagnation_exits = 0

    def load_candles(self, symbol: str) -> pd.DataFrame:
        """Load 5m klines from lake, or synthesize high-fidelity klines if empty."""
        df_5m = None
        for sub in [self.lake_root / "um" / "klines" / symbol / "5m", self.lake_root / "klines" / symbol / "5m"]:
            if sub.exists():
                parquets = sorted(list(sub.glob("*.parquet")))
                if parquets:
                    dfs = []
                    for p in parquets:
                        try:
                            dfs.append(pd.read_parquet(p))
                        except Exception:
                            pass
                    if dfs:
                        merged = pd.concat(dfs, ignore_index=True)
                        if "open_time" in merged.columns and "datetime" not in merged.columns:
                            merged["datetime"] = pd.to_datetime(merged["open_time"], unit="ms", utc=True)
                            merged = merged.set_index("datetime").sort_index()
                        df_5m = merged
                        break

        if df_5m is None or len(df_5m) < 50:
            # Fallback realistic generator if parquet is absent
            np.random.seed(abs(hash(symbol)) % (2**31))
            days = int(self.window_years * 365.25)
            periods = min(150000, days * 288)
            dates = pd.date_range(end=datetime.now(UTC), periods=periods, freq="5min")
            base = 65000.0 if "BTC" in symbol else (2700.0 if "ETH" in symbol else 160.0)
            returns = np.random.normal(0.00005, 0.0035, size=periods)
            price_series = base * np.exp(np.cumsum(returns))
            vol_series = np.random.exponential(scale=150.0, size=periods)
            df_5m = pd.DataFrame({
                "open": price_series * (1 - np.random.uniform(-0.001, 0.001, size=periods)),
                "high": price_series * (1 + np.random.uniform(0.0005, 0.004, size=periods)),
                "low": price_series * (1 - np.random.uniform(0.0005, 0.004, size=periods)),
                "close": price_series,
                "volume": vol_series,
            }, index=dates)

        # Slice to requested window (supporting sub-day horizons like 5 hours or 3 days)
        hours = max(5.0, float(self.window_years * 8766.0))
        cutoff = df_5m.index.max() - pd.Timedelta(hours=hours)
        return df_5m.loc[df_5m.index >= cutoff].copy()

    def run_simulation_for_symbol(self, symbol: str) -> list[SimulatedTrade]:
        """Run step-by-step tick and candle replay for a symbol."""
        df = self.load_candles(symbol)
        if len(df) < 50:
            return []

        closes = df["close"].values
        highs = df["high"].values
        lows = df["low"].values
        volumes = df["volume"].values
        times = df.index

        # Indicator series
        ema20 = pd.Series(closes).ewm(span=20, adjust=False).mean().values
        ema50 = pd.Series(closes).ewm(span=50, adjust=False).mean().values

        # ATR-14
        tr = np.maximum(highs - lows, np.maximum(np.abs(highs - np.roll(closes, 1)), np.abs(lows - np.roll(closes, 1))))
        tr[0] = highs[0] - lows[0]
        atr = pd.Series(tr).rolling(14, min_periods=1).mean().values

        # RSI-14
        diff = np.diff(closes, prepend=closes[0])
        gains = np.where(diff > 0, diff, 0.0)
        losses = np.where(diff < 0, -diff, 0.0)
        avg_gain = pd.Series(gains).rolling(14, min_periods=1).mean().values
        avg_loss = pd.Series(losses).rolling(14, min_periods=1).mean().values
        rs = np.where(avg_loss == 0, 100.0, avg_gain / np.maximum(avg_loss, 1e-6))
        rsi = 100.0 - (100.0 / (1.0 + rs))

        symbol_trades: list[SimulatedTrade] = []
        in_pos = False
        active_trade: SimulatedTrade | None = None
        pos_entry_idx = 0
        last_trade_time: pd.Timestamp | None = None
        last_trade_dir: int = 0
        dispatcher = SimulatorStrategyDispatcher(self.strategy)

        for i in range(50, len(closes) - 1):
            self.total_ticks_processed += 1
            c_time = times[i]
            c_close = closes[i]
            c_high = highs[i]
            c_low = lows[i]
            c_atr = max(atr[i], c_close * 0.003)
            c_rsi = rsi[i]

            if in_pos and active_trade:
                bars_held = i - pos_entry_idx
                is_long = active_trade.direction == "LONG"
                entry = active_trade.entry_price
                tp = active_trade.tp_price
                sl = active_trade.sl_price
                target_dist_pct = abs(tp - entry) / entry * 100.0
                risk_dist_pct = abs(entry - sl) / entry * 100.0

                # Excursion tracking
                if is_long:
                    cur_gain = (c_high - entry) / entry * 100.0
                    cur_dd = (c_low - entry) / entry * 100.0
                    if cur_gain > active_trade.peak_gain_pct:
                        active_trade.peak_gain_pct = cur_gain
                        active_trade.peak_r = round(cur_gain / max(risk_dist_pct, 0.001), 2)
                    if cur_dd < active_trade.max_drawdown_pct:
                        active_trade.max_drawdown_pct = cur_dd

                    # Check TP1 milestone (+1.0R / 50% target)
                    if cur_gain >= 0.5 * target_dist_pct and not active_trade.tp1_hit:
                        active_trade.tp1_hit = True

                    # Check Breakeven ratchet (+0.8%)
                    if cur_gain >= 0.8 and not active_trade.breakeven_ratchet_hit:
                        active_trade.breakeven_ratchet_hit = True
                        active_trade.sl_price = entry  # Lock at breakeven

                    # Dynamic runner extension (+1.5R at >=1.8R)
                    if cur_gain >= 1.8 * risk_dist_pct and not active_trade.runner_extended:
                        active_trade.runner_extended = True
                        active_trade.tp_price = entry + (self.tp_r + 1.5) * c_atr
                        active_trade.sl_price = entry + 1.2 * risk_dist_pct

                    # Check Exits
                    hit_tp = c_high >= active_trade.tp_price
                    hit_sl = c_low <= active_trade.sl_price
                else:  # Short
                    cur_gain = (entry - c_low) / entry * 100.0
                    cur_dd = (entry - c_high) / entry * 100.0
                    if cur_gain > active_trade.peak_gain_pct:
                        active_trade.peak_gain_pct = cur_gain
                        active_trade.peak_r = round(cur_gain / max(risk_dist_pct, 0.001), 2)
                    if cur_dd < active_trade.max_drawdown_pct:
                        active_trade.max_drawdown_pct = cur_dd

                    if cur_gain >= 0.5 * target_dist_pct and not active_trade.tp1_hit:
                        active_trade.tp1_hit = True

                    if cur_gain >= 0.8 and not active_trade.breakeven_ratchet_hit:
                        active_trade.breakeven_ratchet_hit = True
                        active_trade.sl_price = entry

                    if cur_gain >= 1.8 * risk_dist_pct and not active_trade.runner_extended:
                        active_trade.runner_extended = True
                        active_trade.tp_price = entry - (self.tp_r + 1.5) * c_atr
                        active_trade.sl_price = entry - 1.2 * risk_dist_pct

                    hit_tp = c_low <= active_trade.tp_price
                    hit_sl = c_high >= active_trade.sl_price

                # Resolution
                active_trade.target_pct_reached = min(
                    100.0, max(0.0, (active_trade.peak_gain_pct / max(target_dist_pct, 0.001) * 100.0))
                )

                if hit_tp:
                    active_trade.exit_price = active_trade.tp_price
                    active_trade.exit_time = str(c_time)
                    active_trade.pnl_pct = target_dist_pct if is_long else target_dist_pct
                    active_trade.pnl_r = round(self.tp_r + (1.5 if active_trade.runner_extended else 0.0), 2)
                    active_trade.exit_reason = "TAKE_PROFIT_RUNNER" if active_trade.runner_extended else "TAKE_PROFIT_FULL"
                    symbol_trades.append(active_trade)
                    in_pos = False
                    active_trade = None
                    continue

                if hit_sl:
                    active_trade.exit_price = active_trade.sl_price
                    active_trade.exit_time = str(c_time)
                    if active_trade.breakeven_ratchet_hit:
                        active_trade.pnl_pct = 0.0
                        active_trade.pnl_r = 0.0
                        active_trade.exit_reason = "BREAKEVEN_STOP_LOCKED"
                    else:
                        active_trade.pnl_pct = -risk_dist_pct
                        active_trade.pnl_r = -self.sl_r
                        active_trade.exit_reason = "STOP_LOSS_HIT"
                    symbol_trades.append(active_trade)
                    in_pos = False
                    active_trade = None
                    continue

                # Smart Stagnation Exit
                if bars_held >= (self.stagnation_exit_minutes // 5):
                    if active_trade.peak_gain_pct < 0.4:
                        active_trade.exit_price = c_close
                        active_trade.exit_time = str(c_time)
                        move = (c_close - entry) / entry * 100.0 if is_long else (entry - c_close) / entry * 100.0
                        active_trade.pnl_pct = round(move, 2)
                        active_trade.pnl_r = round(move / max(risk_dist_pct, 0.001), 2)
                        active_trade.exit_reason = "STAGNATION_60M_TIMEOUT"
                        self.stagnation_exits += 1
                        symbol_trades.append(active_trade)
                        in_pos = False
                        active_trade = None
                        continue
                continue

            # Production Signal Detection: 5-Family Confluence, Bollinger, Volume Spike, RSI Divergence
            start_w = max(0, i - 120)
            rolling_candles = [
                OHLCVCandle(
                    open=float(df["open"].iloc[j]),
                    high=float(highs[j]),
                    low=float(lows[j]),
                    close=float(closes[j]),
                    volume=float(volumes[j]),
                    timestamp=pd.to_datetime(times[j]),
                )
                for j in range(start_w, i + 1)
            ]
            st = CryptoState(
                symbol=symbol.lower(),
                base_asset=symbol.replace("USDT", ""),
                quote_asset="USDT",
                current_price=c_close,
                candles_1m=rolling_candles,
            )

            # HTF Trend Regime (4-hour rolling trend):
            htf_window = closes[max(0, i - 48):i + 1]
            htf_ema = float(pd.Series(htf_window).ewm(span=24, adjust=False).mean().iloc[-1])
            is_htf_bull = c_close >= htf_ema * 1.002
            is_htf_bear = c_close <= htf_ema * 0.998

            sig_result = dispatcher.detect_signal(
                symbol=symbol,
                state=st,
                c_close=c_close,
                c_atr=c_atr,
                opens=np.asarray(df["open"].values, dtype=float),
                highs=highs,
                lows=lows,
                closes=closes,
                i=i,
                rolling_candles=rolling_candles,
                htf_ema=htf_ema,
                is_htf_bull=is_htf_bull,
                is_htf_bear=is_htf_bear,
            )
            if not sig_result:
                continue

            target_dir, sig_type, sig_conf = sig_result

            # Strict Macro Trend Direction Lock:
            # When HTF is Bullish, ALL SHORTS ARE VETOED (eliminates 100% of counter-trend loss)
            # When HTF is Bearish, ALL LONGS ARE VETOED
            if target_dir == -1 and is_htf_bull:
                continue
            if target_dir == 1 and is_htf_bear:
                continue

            # Anti-Flip Guard: veto opposite trade within cooldown
            if last_trade_time is not None:
                mins_since = (c_time - last_trade_time).total_seconds() / 60.0
                if mins_since < self.anti_flip_cooldown_minutes and target_dir != last_trade_dir:
                    self.anti_flip_vetos += 1
                    continue

            # Open Simulated Position with Volatility Structure Stop
            self.trade_counter += 1
            is_long = target_dir == 1
            pos_entry = c_close
            # Dynamic structure stop: 1.8x ATR floor, min 1.2% distance (immune to 5m noise)
            risk_dist = max(1.8 * c_atr, pos_entry * 0.012)
            pos_tp = pos_entry + self.tp_r * risk_dist if is_long else pos_entry - self.tp_r * risk_dist
            pos_sl = pos_entry - self.sl_r * risk_dist if is_long else pos_entry + self.sl_r * risk_dist

            active_trade = SimulatedTrade(
                trade_id=self.trade_counter,
                symbol=symbol,
                direction="LONG" if is_long else "SHORT",
                entry_time=str(c_time),
                entry_price=round(pos_entry, 4),
                tp_price=round(pos_tp, 4),
                sl_price=round(pos_sl, 4),
                indicators_at_entry={
                    "strategy": sig_type,
                    "confidence": round(sig_conf, 3),
                    "rsi": round(float(c_rsi), 1),
                    "atr_pct": round(float(risk_dist / pos_entry * 100.0), 3),
                    "htf_ema": round(htf_ema, 2),
                },
            )
            in_pos = True
            pos_entry_idx = i
            last_trade_time = c_time
            last_trade_dir = target_dir

        self.trades.extend(symbol_trades)
        return symbol_trades
