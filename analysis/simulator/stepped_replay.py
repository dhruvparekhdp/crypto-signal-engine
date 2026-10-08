"""
Stepped Time-Paced Market Replay Engine.

Replays multi-year market data with configurable clock pacing:
- Configurable Market Hours per step (e.g. 1.0 hr, 2.0 hr, 0.5 hr)
- Configurable Analysis Budget per step (e.g. 60s, 30s, 15s)
- Pacing controller: sleeps smoothly with sub-second dashboard countdowns if early;
  takes all needed time without extra delay if overtime.
- Full pipeline per step:
  1. Ingest multi-pair candles & ticks
  2. Multi-strategy signal detection (Primary confluence, breakout, scalp)
  3. Mirror signal trade checks & tracking
  4. Dual post-mortem AI evaluations (Gemini, Groq, OpenRouter, HF)
  5. Compounding challenge updates ($25 -> $100 cycles)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
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
from analysis.crypto_state import CryptoState, OHLCVCandle
from analysis.simulator.ai_orchestrator import DualEngineAIOrchestrator
from analysis.simulator.cycle_challenge import run_cycle_simulation
from analysis.simulator.global_ai_governor import governor
from analysis.simulator.replay_engine import MarketReplayEngine, SimulatedTrade
from analysis.simulator.strategy_dispatcher import SimulatorStrategyDispatcher

log = logging.getLogger("simulator.stepped")


class SteppedMarketReplayEngine:
    """
    Stepped, clock-synchronized market simulator executing multi-asset
    signal detection, mirror evaluations, AI thinking, and cycle compounding.
    """

    def __init__(
        self,
        symbols: list[str],
        window_years: float = 0.083,  # Default 1 month for paced runs
        lake_root: str = "data/lake",
        market_step_hours: float = 1.0,
        step_budget_seconds: float = 60.0,
        tp_r: float = 2.0,
        sl_r: float = 1.2,
        anti_flip_cooldown_minutes: int = 90,
        stagnation_exit_minutes: int = 60,
        cycle_start: float = 25.0,
        cycle_target: float = 100.0,
        cycle_margin_pct: float = 0.25,
        cycle_leverage: float = 10.0,
        strategy: str = "all",
        ai_orchestrator: DualEngineAIOrchestrator | None = None,
        status_file_path: str = "data/simulator/status.json",
        stop_requested_checker: Callable[[], bool] | None = None,
    ):
        self.symbols = [s.upper() for s in symbols]
        self.window_years = window_years
        self.lake_root = Path(lake_root)
        self.market_step_hours = max(0.1, market_step_hours)
        self.step_budget_seconds = max(1.0, step_budget_seconds)
        self.tp_r = tp_r
        self.sl_r = sl_r
        self.anti_flip_cooldown_minutes = anti_flip_cooldown_minutes
        self.stagnation_exit_minutes = stagnation_exit_minutes
        self.cycle_start = cycle_start
        self.cycle_target = cycle_target
        self.cycle_margin_pct = cycle_margin_pct
        self.cycle_leverage = cycle_leverage
        self.strategy = (strategy or "all").lower().strip()
        self.ai = ai_orchestrator or DualEngineAIOrchestrator()
        self.status_file_path = Path(status_file_path)
        self.status_file_path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_requested_checker = stop_requested_checker

        self.base_engine = MarketReplayEngine(
            symbols=self.symbols,
            window_years=self.window_years,
            lake_root=str(self.lake_root),
            tp_r=self.tp_r,
            sl_r=self.sl_r,
            anti_flip_cooldown_minutes=self.anti_flip_cooldown_minutes,
            stagnation_exit_minutes=self.stagnation_exit_minutes,
            strategy=self.strategy,
        )
        self.dispatcher = SimulatorStrategyDispatcher(self.strategy)

        self.all_simulated_trades: list[SimulatedTrade] = []
        self.recent_streamed_trades: list[dict[str, Any]] = []

    def load_all_market_data(self) -> dict[str, pd.DataFrame]:
        """Pre-load candles for all symbols into memory and normalize datetime."""
        market_data: dict[str, pd.DataFrame] = {}
        for sym in self.symbols:
            df = self.base_engine.load_candles(sym)
            if not df.empty:
                df = df.copy()
                if isinstance(df.index, pd.DatetimeIndex):
                    df["open_time"] = df.index
                elif "open_time" in df.columns:
                    if isinstance(df["open_time"].iloc[0], (int, np.integer)):
                        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
                    else:
                        df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
                market_data[sym] = df
        return market_data

    async def run_stepped_simulation(self) -> dict[str, Any]:
        """Execute the paced step-by-step simulation loop."""
        sim_start_time = time.time()
        market_dfs = self.load_all_market_data()
        if not market_dfs:
            raise RuntimeError("No market candle data available to simulate.")

        # Determine global time range across all symbols
        all_times = []
        for df in market_dfs.values():
            if "open_time" in df.columns:
                all_times.extend(df["open_time"].tolist())
        all_times = sorted(list(set(all_times)))
        if not all_times:
            raise RuntimeError("Candle data contains no open_time timestamps.")

        min_time = pd.Timestamp(all_times[0])
        max_time = pd.Timestamp(all_times[-1])
        total_market_span = max_time - min_time
        total_market_hours = total_market_span.total_seconds() / 3600.0
        total_steps = max(1, int(total_market_hours / self.market_step_hours))

        # Expected run duration
        expected_total_duration_sec = total_steps * self.step_budget_seconds
        projected_finish_utc = datetime.now(UTC) + timedelta(seconds=expected_total_duration_sec)

        log.info(
            "stepped_sim_starting",
            total_steps=total_steps,
            step_hours=self.market_step_hours,
            budget_sec=self.step_budget_seconds,
            est_duration_min=round(expected_total_duration_sec / 60.0, 1),
        )

        current_window_start = min_time
        step_idx = 0

        # State tracking per symbol for in-flight positions across steps
        symbol_positions: dict[str, SimulatedTrade | None] = {s: None for s in self.symbols}
        symbol_last_trade_time: dict[str, pd.Timestamp | None] = {s: None for s in self.symbols}
        symbol_last_trade_dir: dict[str, int] = {s: 0 for s in self.symbols}

        status: dict[str, Any] = {
            "is_running": True,
            "stepped_mode": True,
            "current_step": 0,
            "total_steps": total_steps,
            "step_market_hours": self.market_step_hours,
            "step_budget_seconds": self.step_budget_seconds,
            "progress_pct": 0.0,
            "status": "running",
            "current_market_time": current_window_start.strftime("%Y-%m-%d %H:%M UTC"),
            "step_seconds_elapsed": 0.0,
            "is_overtime": False,
            "estimated_finish_time": projected_finish_utc.strftime("%Y-%m-%d %H:%M UTC"),
            "trades_simulated": 0,
            "won_count": 0,
            "partial_count": 0,
            "stopped_count": 0,
            "stagnated_count": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 1.0,
            "cycle_challenge": {},
            "recent_trades": [],
            "ai_telemetry": governor.get_telemetry(),
        }
        self._write_status(status)

        while current_window_start < max_time:
            if self.stop_requested_checker and self.stop_requested_checker():
                log.info("stepped_sim_stop_requested", step=step_idx)
                break

            step_idx += 1
            step_start_perf = time.perf_counter()
            next_window_start = current_window_start + timedelta(hours=self.market_step_hours)
            status["current_step"] = step_idx
            status["current_market_time"] = current_window_start.strftime("%Y-%m-%d %H:%M UTC")

            step_trades_closed: list[SimulatedTrade] = []

            # Process price action for each symbol in this step window
            for sym, df in market_dfs.items():
                mask = (df["open_time"] >= current_window_start) & (df["open_time"] < next_window_start)
                step_df = df.loc[mask]
                if step_df.empty:
                    continue

                active_trade = symbol_positions[sym]
                last_time = symbol_last_trade_time[sym]
                last_dir = symbol_last_trade_dir[sym]

                # Run step simulation on symbol slice
                closed_trades, updated_pos, new_last_time, new_last_dir = self._process_symbol_step(
                    sym, step_df, active_trade, last_time, last_dir
                )

                symbol_positions[sym] = updated_pos
                symbol_last_trade_time[sym] = new_last_time
                symbol_last_trade_dir[sym] = new_last_dir
                step_trades_closed.extend(closed_trades)

            # Evaluate closed trades with Multi-Provider AI (Gemini, Groq, OpenRouter, HF)
            for trade in step_trades_closed:
                await self.ai.evaluate_trade(trade, call_type="dual_post_mortem")
                self.all_simulated_trades.append(trade)
                self.recent_streamed_trades.insert(
                    0,
                    {
                        "id": trade.trade_id,
                        "symbol": trade.symbol,
                        "direction": trade.direction,
                        "entry_time": trade.entry_time,
                        "exit_time": trade.exit_time,
                        "entry_price": trade.entry_price,
                        "exit_price": trade.exit_price,
                        "tp_price": trade.tp_price,
                        "sl_price": trade.sl_price,
                        "pnl_pct": trade.pnl_pct,
                        "pnl_r": trade.pnl_r,
                        "exit_reason": trade.exit_reason,
                        "peak_gain_pct": trade.peak_gain_pct,
                        "peak_r": trade.peak_r,
                        "target_pct_reached": trade.target_pct_reached,
                        "max_drawdown_pct": trade.max_drawdown_pct,
                        "why_it_worked": trade.ai_why_it_worked,
                        "why_it_failed": trade.ai_why_it_failed,
                        "key_takeaway": trade.ai_key_takeaway,
                        "thinking_trace": trade.ai_thinking_trace,
                        "source_model": trade.ai_source_model,
                    },
                )

                # R3: these two were never defined, so the first closed trade raised NameError
                from analysis.simulator.cycle_challenge import run_cycle_simulation
                sorted_trades = sorted(self.all_simulated_trades, key=lambda t: str(t.entry_time))
                cycle_results = run_cycle_simulation(sorted_trades)

                # Save full cycle statements to disk for detailed audits
                cycle_report_path = Path("data/simulator/reports/cycle_statements.json")
                cycle_report_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    cycle_report_path.write_text(json.dumps(cycle_results, indent=2))
                except Exception:
                    pass

                # Keep status["cycle_challenge"] lightweight for fast mobile streaming
                lightweight_cycles = []
                for c in cycle_results.get("cycles", []):
                    c_summary = {k: v for k, v in c.items() if k != "transactions"}
                    c_summary["transaction_count"] = len(c.get("transactions", []))
                    lightweight_cycles.append(c_summary)

                status["cycle_challenge"] = {
                    k: v for k, v in cycle_results.items() if k != "cycles"
                }
                status["cycle_challenge"]["cycles"] = lightweight_cycles

                won = sum(1 for t in sorted_trades if t.pnl_r > 0)
                stopped = sum(1 for t in sorted_trades if "STOP" in t.exit_reason)
                stagnated = sum(1 for t in sorted_trades if "STAGNATION" in t.exit_reason)
                partial = sum(1 for t in sorted_trades if t.target_pct_reached >= 40.0 and t.pnl_r <= 0)
                gross_profit = sum(t.pnl_r for t in sorted_trades if t.pnl_r > 0)
                gross_loss = abs(sum(t.pnl_r for t in sorted_trades if t.pnl_r < 0))
                profit_factor = round(gross_profit / max(gross_loss, 0.001), 2)
                win_rate = round(won / max(1, len(sorted_trades)) * 100.0, 1)

                status["trades_simulated"] = len(sorted_trades)
                status["won_count"] = won
                status["partial_count"] = partial
                status["stopped_count"] = stopped
                status["stagnated_count"] = stagnated
                status["win_rate_pct"] = win_rate
                status["profit_factor"] = profit_factor

            status["recent_trades"] = self.recent_streamed_trades[:25]
            status["progress_pct"] = round((step_idx / total_steps) * 100.0, 1)
            status["ai_telemetry"] = governor.get_telemetry()

            # Pacing Controller (1hr market = 1min analysis or custom budget)
            elapsed_step_sec = time.perf_counter() - step_start_perf
            if elapsed_step_sec < self.step_budget_seconds:
                status["is_overtime"] = False
                # Sleep smoothly in small ticks while updating live countdown bar
                remaining_budget = self.step_budget_seconds - elapsed_step_sec
                wake_target = time.perf_counter() + remaining_budget
                while time.perf_counter() < wake_target:
                    if self.stop_requested_checker and self.stop_requested_checker():
                        break
                    cur_elapsed = time.perf_counter() - step_start_perf
                    status["step_seconds_elapsed"] = round(cur_elapsed, 1)
                    status["step_countdown_remaining"] = max(0, int(self.step_budget_seconds - cur_elapsed))
                    self._write_status(status)
                    await asyncio.sleep(0.5)
            else:
                # Overtime: Took more time as instructed, advance immediately to next step
                status["is_overtime"] = True
                status["step_seconds_elapsed"] = round(elapsed_step_sec, 1)
                status["step_countdown_remaining"] = 0
                self._write_status(status)

            current_window_start = next_window_start

        status["is_running"] = False
        status["status"] = "completed"
        status["progress_pct"] = 100.0
        self._write_status(status)
        return status

    def _process_symbol_step(
        self,
        symbol: str,
        df: pd.DataFrame,
        active_trade: SimulatedTrade | None,
        last_trade_time: pd.Timestamp | None,
        last_trade_dir: int,
    ) -> tuple[list[SimulatedTrade], SimulatedTrade | None, pd.Timestamp | None, int]:
        """Process candles for a single symbol within the step window."""
        closed_trades: list[SimulatedTrade] = []
        closes = df["close"].values
        highs = df["high"].values
        lows = df["low"].values
        opens = df["open"].values
        times = df["open_time"].values

        for i in range(len(closes)):
            c_time = pd.Timestamp(times[i])
            c_close = float(closes[i])
            c_high = float(highs[i])
            c_low = float(lows[i])
            c_atr = max(c_high - c_low, c_close * 0.004)

            # In-position management
            if active_trade:
                is_long = active_trade.direction == "LONG"
                entry = active_trade.entry_price
                risk_dist = abs(entry - active_trade.sl_price)
                target_dist = abs(active_trade.tp_price - entry)

                # Excursion tracking
                if is_long:
                    cur_gain = (c_high - entry) / entry * 100.0
                    cur_dd = (c_low - entry) / entry * 100.0
                    if cur_gain > active_trade.peak_gain_pct:
                        active_trade.peak_gain_pct = round(cur_gain, 2)
                        active_trade.peak_r = round(cur_gain / max(risk_dist / entry * 100.0, 0.001), 2)
                    if cur_dd < active_trade.max_drawdown_pct:
                        active_trade.max_drawdown_pct = round(cur_dd, 2)

                    # Breakeven ratchet (+0.8R)
                    if cur_gain >= 0.8 and not active_trade.breakeven_ratchet_hit:
                        active_trade.breakeven_ratchet_hit = True
                        active_trade.sl_price = entry

                    # Check Exits
                    hit_tp = c_high >= active_trade.tp_price
                    hit_sl = c_low <= active_trade.sl_price
                else:
                    cur_gain = (entry - c_low) / entry * 100.0
                    cur_dd = (entry - c_high) / entry * 100.0
                    if cur_gain > active_trade.peak_gain_pct:
                        active_trade.peak_gain_pct = round(cur_gain, 2)
                        active_trade.peak_r = round(cur_gain / max(risk_dist / entry * 100.0, 0.001), 2)
                    if cur_dd < active_trade.max_drawdown_pct:
                        active_trade.max_drawdown_pct = round(cur_dd, 2)

                    if cur_gain >= 0.8 and not active_trade.breakeven_ratchet_hit:
                        active_trade.breakeven_ratchet_hit = True
                        active_trade.sl_price = entry

                    hit_tp = c_low <= active_trade.tp_price
                    hit_sl = c_high >= active_trade.sl_price

                if hit_tp and not hit_sl:            # R4: both touched in one bar -> the stop counts
                    active_trade.exit_time = c_time.isoformat()
                    active_trade.exit_price = active_trade.tp_price
                    active_trade.exit_reason = f"TAKE_PROFIT_{self.tp_r}R"
                    active_trade.pnl_r = self.tp_r
                    active_trade.pnl_pct = round(abs(active_trade.exit_price - entry) / entry * 100.0, 2)
                    active_trade.target_pct_reached = 100.0
                    closed_trades.append(active_trade)
                    active_trade = None
                    last_trade_time = c_time
                elif hit_sl:
                    active_trade.exit_time = c_time.isoformat()
                    active_trade.exit_price = active_trade.sl_price
                    active_trade.exit_reason = f"STOP_LOSS_{self.sl_r}R"
                    active_trade.pnl_r = -self.sl_r if not active_trade.breakeven_ratchet_hit else 0.0
                    active_trade.pnl_pct = round(-abs(entry - active_trade.exit_price) / entry * 100.0, 2)
                    closed_trades.append(active_trade)
                    active_trade = None
                    last_trade_time = c_time

            # Production Signal Detection: 5-Family Confluence, Bollinger, Volume Spike, RSI Divergence
            if not active_trade and i > 5:
                start_w = max(0, i - 120)
                rolling_candles = [
                    OHLCVCandle(
                        open=float(opens[j]),
                        high=float(highs[j]),
                        low=float(lows[j]),
                        close=float(closes[j]),
                        volume=float(df["volume"].iloc[j]) if "volume" in df.columns else 100.0,
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

                # Higher-Timeframe Trend (24-period EMA on rolling bars):
                htf_window = closes[max(0, i - 48):i + 1]
                htf_ema = float(pd.Series(htf_window).ewm(span=24, adjust=False).mean().iloc[-1])
                is_htf_bull = c_close >= htf_ema * 1.002
                is_htf_bear = c_close <= htf_ema * 0.998

                sig_result = self.dispatcher.detect_signal(
                    symbol=symbol,
                    state=st,
                    c_close=c_close,
                    c_atr=c_atr,
                    opens=opens,
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

                target_dir, matched_sig_type, matched_sig_conf = sig_result

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
                        continue

                # Open Simulated Position with Volatility Structure Stop
                self.base_engine.trade_counter += 1
                is_long = target_dir == 1
                pos_entry = c_close
                # Dynamic structure stop: 1.8x ATR floor, min 1.2% distance (immune to 5m noise)
                risk_dist = max(1.8 * c_atr, pos_entry * 0.012)
                pos_tp = pos_entry + self.tp_r * risk_dist if is_long else pos_entry - self.tp_r * risk_dist
                pos_sl = pos_entry - self.sl_r * risk_dist if is_long else pos_entry + self.sl_r * risk_dist

                active_trade = SimulatedTrade(
                    trade_id=self.base_engine.trade_counter,
                    symbol=symbol,
                    direction="LONG" if is_long else "SHORT",
                    entry_time=c_time.isoformat(),
                    entry_price=round(pos_entry, 4),
                    tp_price=round(pos_tp, 4),
                    sl_price=round(pos_sl, 4),
                    indicators_at_entry={
                        "strategy": matched_sig_type,
                        "confidence": round(matched_sig_conf, 3),
                        "atr": round(c_atr, 4),
                        "price": round(c_close, 4),
                        "htf_ema": round(htf_ema, 2),
                    },
                )
                last_trade_dir = target_dir

        return closed_trades, active_trade, last_trade_time, last_trade_dir

    def _write_status(self, status: dict[str, Any]) -> None:
        """Atomically persist status to disk."""
        tmp = self.status_file_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(status, indent=2))
            tmp.replace(self.status_file_path)
        except Exception as e:
            log.warning("failed_to_write_stepped_status", error=str(e))
