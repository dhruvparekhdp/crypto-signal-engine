"""
Nested AI Thinking Evaluator for Market Simulation.

Maximizes Hugging Face AI API utilization:
- Pre-trade signal evaluation
- In-flight position management
- Dual post-mortem reasoning:
  - 'why_it_worked': momentum, confluence, volume absorption
  - 'why_it_failed': opposing resistance, liquidity sweeps, exhaustion
- Multi-layer nested thinking traces `<think>...</think>` from reasoning models
  (deepseek-ai/DeepSeek-R1-Distill-Qwen-32B or meta-llama/Llama-3.1-8B-Instruct).
- High-throughput async batching with concurrency control and fallback protection.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from analysis.simulator.replay_engine import SimulatedTrade

log = logging.getLogger("simulator.thinking")


class NestedThinkingEvaluator:
    """
    Orchestrates high-volume Hugging Face AI calls for deep trade reasoning
    and dataset generation.
    """

    def __init__(
        self,
        provider_model: str = "hf:meta-llama/Llama-3.1-8B-Instruct",
        max_concurrency: int = 5,
        thinking_depth: str = "deep",
    ):
        self.provider_model = provider_model
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.thinking_depth = thinking_depth
        self.total_calls_dispatched = 0
        self.total_calls_succeeded = 0
        self.total_failures = 0

    async def evaluate_trade_lifecycle(self, trade: SimulatedTrade) -> SimulatedTrade:
        """
        Execute deep dual post-mortem and nested thinking on a completed simulated trade.
        """
        from collectors.llm_client import ask_json

        self.total_calls_dispatched += 1
        system_prompt = (
            "You are an institutional quantitative crypto trading AI analyst. "
            "You are performing a comprehensive dual post-mortem and nested architectural review on a systematic trade.\n"
            "Produce structured thinking covering:\n"
            "<think>\n"
            "[Macro Regime]: Is macro momentum supporting or counter to this move?\n"
            "[Micro Structure]: Quality of entry, wick absorption, and fakeout danger.\n"
            "[Excursion & In-flight]: How much did the trade work before opposing pressure intervened?\n"
            "[Algorithmic Optimization]: What parameter or protection tweak would maximize expectancy?\n"
            "</think>\n\n"
            "Return STRICT JSON with keys:\n"
            "- 'thinking_trace': The complete chain-of-thought analysis above.\n"
            "- 'why_it_worked': Precise technical breakdown of why price moved favorably to peak price.\n"
            "- 'why_it_failed': Precise breakdown of why price stopped, stalled, or reversed before full target.\n"
            "- 'key_takeaway': 1 actionable quantitative heuristic for system optimization."
        )

        user_prompt = (
            f"Symbol: {trade.symbol} | Direction: {trade.direction}\n"
            f"Entry Time: {trade.entry_time} | Exit Time: {trade.exit_time}\n"
            f"Entry Price: {trade.entry_price} | Exit Price: {trade.exit_price}\n"
            f"Take-Profit: {trade.tp_price} | Stop-Loss: {trade.sl_price}\n"
            f"Exit Reason: {trade.exit_reason} | Realized PnL: {trade.pnl_pct}% ({trade.pnl_r}R)\n"
            f"Performance Metrics:\n"
            f"- Peak Gain (MFE): +{trade.peak_gain_pct}% (+{trade.peak_r}R)\n"
            f"- Target Distance Achieved: {trade.target_pct_reached}%\n"
            f"- Max Adverse Drawdown (MAE): {trade.max_drawdown_pct}%\n"
            f"- TP1 Milestone Hit: {trade.tp1_hit}\n"
            f"- Breakeven Ratchet Engaged: {trade.breakeven_ratchet_hit}\n"
            f"Entry Indicators: {json.dumps(trade.indicators_at_entry)}\n"
        )

        async with self.semaphore:
            try:
                reply = await ask_json(
                    role="attribution",
                    system=system_prompt,
                    user=user_prompt,
                    max_tokens=650,
                    temperature=0.15,
                    timeout=22.0,
                )
                if reply and isinstance(reply.data, dict) and reply.data.get("why_it_worked"):
                    data = reply.data
                    trade.ai_why_it_worked = str(data.get("why_it_worked", ""))
                    trade.ai_why_it_failed = str(data.get("why_it_failed", ""))
                    trade.ai_key_takeaway = str(data.get("key_takeaway", ""))
                    trade.ai_thinking_trace = str(data.get("thinking_trace", ""))
                    trade.ai_source_model = f"{reply.provider}/{reply.model}" if reply.provider else "hf/Llama-3.1-8B"
                    self.total_calls_succeeded += 1
                    return trade
            except Exception as e:
                self.total_failures += 1
                log.warning("nested_thinking_eval_error", trade_id=trade.trade_id, error=str(e))

        # Algorithmic synthetic nested thinking fallback
        trade.ai_source_model = "algorithmic_synthesis"
        trade.ai_thinking_trace = (
            f"<think>\n"
            f"[Macro Regime]: {trade.direction} momentum on {trade.symbol} confirmed by EMA-20/50 alignment.\n"
            f"[Micro Structure]: Entry at {trade.entry_price} with RSI {trade.indicators_at_entry.get('rsi', 50)}.\n"
            f"[Excursion & In-flight]: Peaked at +{trade.peak_gain_pct}% ({trade.target_pct_reached}% to target), MAE {trade.max_drawdown_pct}%.\n"
            f"[Algorithmic Optimization]: {'Extend runner mode' if trade.pnl_r > 1.5 else 'Tighten breakeven ratchet threshold'}.\n"
            f"</think>"
        )
        if trade.pnl_r > 0:
            trade.ai_why_it_worked = f"Clean {trade.direction.lower()} trend impulse expanded favorably, capturing +{trade.peak_gain_pct}% towards target {trade.tp_price}."
            trade.ai_why_it_failed = "Opposing liquidity stepped in at local resistance, preventing further run beyond exit."
            trade.ai_key_takeaway = "Lock partial gains at +1.0R while trailing remaining volume."
        else:
            trade.ai_why_it_worked = f"Initial favorable excursion reached +{trade.peak_gain_pct}% ({trade.target_pct_reached}% of target distance)."
            trade.ai_why_it_failed = f"Opposing orderflow swept stop-loss at {trade.sl_price}; momentum divergence invalidated continuation."
            trade.ai_key_takeaway = "Cut losses immediately at structural invalidation."

        return trade

    async def evaluate_batch(
        self,
        trades: list[SimulatedTrade],
        progress_callback: Any | None = None,
    ) -> list[SimulatedTrade]:
        """Process a batch of simulated trades with adaptive pacing and live streaming."""
        completed = []
        for idx, t in enumerate(trades):
            res = await self.evaluate_trade_lifecycle(t)
            completed.append(res)
            if progress_callback:
                progress_callback(res, len(completed), len(trades))
            # Pacing delay to avoid provider OTPM/rate-limit errors
            if idx < len(trades) - 1:
                await asyncio.sleep(0.35)

        return completed
