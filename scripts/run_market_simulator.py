"""
Background Runner & Controller for 3-Year Live Market Simulator.

Replays historic tick data, simulates systematic trades,
runs maximum Hugging Face AI calls for nested thinking & dual post-mortem,
and writes live progress telemetry to data/simulator/status.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from analysis.simulator.replay_engine import MarketReplayEngine, SimulatedTrade
from analysis.simulator.thinking_evaluator import NestedThinkingEvaluator

log = logging.getLogger("simulator.runner")
STATUS_PATH = Path("data/simulator/status.json")
REPORT_PATH = Path("data/simulator/reports/trades_3y_hf.json")
CYCLE_REPORT_PATH = Path("data/simulator/reports/cycle_statements.json")


def update_status(data: dict) -> None:
    """Atomically write status for live web dashboard streaming."""
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATUS_PATH.with_suffix(".tmp")
    data["updated_at"] = datetime.now(UTC).isoformat()
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    temp.replace(STATUS_PATH)


async def main():
    parser = argparse.ArgumentParser(description="3-Year Live Market Simulator with Hugging Face AI")
    parser.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,AVAXUSDT,LINKUSDT")
    parser.add_argument("--years", type=float, default=3.0)
    parser.add_argument("--tp-r", type=float, default=2.0)
    parser.add_argument("--sl-r", type=float, default=1.2)
    parser.add_argument("--anti-flip", type=int, default=90)
    parser.add_argument("--stagnation", type=int, default=60)
    parser.add_argument("--max-ai-reviews", type=int, default=300)
    parser.add_argument("--lake-root", type=str, default="data/lake")
    parser.add_argument("--cycle-start", type=float, default=25.0)
    parser.add_argument("--cycle-target", type=float, default=100.0)
    parser.add_argument("--cycle-margin-pct", type=float, default=0.25)
    parser.add_argument("--cycle-leverage", type=float, default=10.0)
    parser.add_argument("--stepped-mode", action="store_true", default=False, help="Run in clock-synchronized stepped mode")
    parser.add_argument("--step-market-hours", type=float, default=1.0, help="Market hours per step")
    parser.add_argument("--step-seconds", type=float, default=60.0, help="Wall-clock analysis budget per step")
    parser.add_argument("--strategy", type=str, default="all", help="all, confluence, bollinger_squeeze, volume_spike, rsi_divergence, trend_pullback, range_breakout, sweep_reclaim, breakout_retest")
    # Keys come from the environment (SIM_GEMINI_KEY, SIM_HF_TOKENS, SIM_OPENROUTER_KEY): command-line arguments
    # are visible to every user in the process list (plan finding L11). The flags remain for manual runs.
    parser.add_argument("--gemini-key", type=str, default=os.environ.get("SIM_GEMINI_KEY", ""), help="Gemini API Key")
    parser.add_argument("--hf-tokens", type=str, default=os.environ.get("SIM_HF_TOKENS", ""), help="Hugging Face free token(s)")
    parser.add_argument("--openrouter-key", type=str, default=os.environ.get("SIM_OPENROUTER_KEY", ""), help="OpenRouter API Key")
    parser.add_argument("--ai-provider", type=str, default="none", help="none (0 API calls), auto, gemini, groq, hf, openrouter")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start_time = time.time()

    status = {
        "is_running": True,
        "progress_pct": 0.0,
        "symbols": symbols,
        "window_years": args.years,
        "ticks_processed": 0,
        "trades_simulated": 0,
        "won_count": 0,
        "partial_count": 0,
        "stopped_count": 0,
        "stagnated_count": 0,
        "win_rate_pct": 0.0,
        "profit_factor": 1.0,
        "max_drawdown_r": 0.0,
        "hf_calls_dispatched": 0,
        "hf_calls_succeeded": 0,
        "elapsed_seconds": 0,
        "current_symbol": "",
        "recent_trades": [],
    }
    update_status(status)

    if args.stepped_mode:
        print(f"⏱️ [STEPPED SIMULATOR] Launching Clock-Paced Market Simulator across {len(symbols)} symbols...")
        print(f"   Step Market Hours: {args.step_market_hours}h | Analysis Budget: {args.step_seconds}s per step | AI: {args.ai_provider}")
        from analysis.simulator.ai_orchestrator import DualEngineAIOrchestrator
        from analysis.simulator.stepped_replay import SteppedMarketReplayEngine

        ai_orch = DualEngineAIOrchestrator(
            gemini_key=args.gemini_key,
            hf_token=args.hf_tokens,
            openrouter_key=args.openrouter_key,
            preferred_provider=args.ai_provider,
        )
        stepped_engine = SteppedMarketReplayEngine(
            symbols=symbols,
            window_years=args.years,
            lake_root=args.lake_root,
            market_step_hours=args.step_market_hours,
            step_budget_seconds=args.step_seconds,
            tp_r=args.tp_r,
            sl_r=args.sl_r,
            anti_flip_cooldown_minutes=args.anti_flip,
            stagnation_exit_minutes=args.stagnation,
            cycle_start=args.cycle_start,
            cycle_target=args.cycle_target,
            cycle_margin_pct=args.cycle_margin_pct,
            cycle_leverage=args.cycle_leverage,
            strategy=args.strategy,
            ai_orchestrator=ai_orch,
            status_file_path=str(STATUS_PATH),
        )
        res = await stepped_engine.run_stepped_simulation()
        print(f"🏁 [STEPPED SIMULATOR] Finished {res.get('current_step', 0)} steps. Trades: {res.get('trades_simulated', 0)} | Win Rate: {res.get('win_rate_pct', 0)}%")
        return

    print(f"🚀 [SIMULATOR] Launching {args.years}-Year Live Market Simulator across {len(symbols)} symbols...")
    print(f"   Strategy: {args.strategy} | Take-Profit: {args.tp_r}R | Stop-Loss: {args.sl_r}R | Anti-Flip: {args.anti_flip}m | Stagnation: {args.stagnation}m")

    engine = MarketReplayEngine(
        symbols=symbols,
        window_years=args.years,
        lake_root=args.lake_root,
        tp_r=args.tp_r,
        sl_r=args.sl_r,
        anti_flip_cooldown_minutes=args.anti_flip,
        stagnation_exit_minutes=args.stagnation,
        strategy=args.strategy,
    )

    all_trades: list[SimulatedTrade] = []
    total_sym = len(symbols)

    for idx, sym in enumerate(symbols):
        status["current_symbol"] = sym
        status["progress_pct"] = round((idx / total_sym) * 60.0, 1)
        status["ticks_processed"] = engine.total_ticks_processed
        status["elapsed_seconds"] = int(time.time() - start_time)
        update_status(status)

        print(f"⏳ [SIMULATOR] Replaying {sym} ({idx+1}/{total_sym})...")
        sym_trades = engine.run_simulation_for_symbol(sym)
        all_trades.extend(sym_trades)
        print(f"   ↳ Generated {len(sym_trades)} simulated trades for {sym}.")

    # Calculate aggregate performance
    won = sum(1 for t in all_trades if t.pnl_r > 0)
    stopped = sum(1 for t in all_trades if "STOP" in t.exit_reason)
    stagnated = sum(1 for t in all_trades if "STAGNATION" in t.exit_reason)
    partial = sum(1 for t in all_trades if t.target_pct_reached >= 40.0 and t.pnl_r <= 0)

    gross_profit = sum(t.pnl_r for t in all_trades if t.pnl_r > 0)
    gross_loss = abs(sum(t.pnl_r for t in all_trades if t.pnl_r < 0))
    profit_factor = round(gross_profit / max(gross_loss, 0.001), 2)
    win_rate = round(won / max(1, len(all_trades)) * 100.0, 1)

    # Sort trades chronologically for paper trade account ledger simulation
    all_trades.sort(key=lambda t: str(t.entry_time))

    from analysis.simulator.cycle_challenge import run_cycle_simulation
    cycle_results = run_cycle_simulation(
        all_trades,
        start_balance=args.cycle_start,
        target_balance=args.cycle_target,
        margin_pct=args.cycle_margin_pct,
        leverage=args.cycle_leverage,
    )

    # Save full cycle report (with full transaction history)
    CYCLE_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CYCLE_REPORT_PATH, "w") as f:
        json.dump(cycle_results, f, indent=2)

    # Keep status["cycle_challenge"] lightweight for fast mobile UI
    lightweight_cycles = []
    for c in cycle_results.get("cycles", []):
        c_summary = {k: v for k, v in c.items() if k != "transactions"}
        c_summary["transaction_count"] = len(c.get("transactions", []))
        lightweight_cycles.append(c_summary)

    status["cycle_challenge"] = {k: v for k, v in cycle_results.items() if k != "cycles"}
    status["cycle_challenge"]["cycles"] = lightweight_cycles

    status["trades_simulated"] = len(all_trades)
    status["won_count"] = won
    status["partial_count"] = partial
    status["stopped_count"] = stopped
    status["stagnated_count"] = stagnated
    status["win_rate_pct"] = win_rate
    status["profit_factor"] = profit_factor
    status["progress_pct"] = 65.0
    update_status(status)

    print(f"\n📊 [SIMULATOR STATS] {len(all_trades)} trades simulated. Win Rate: {win_rate}% | Profit Factor: {profit_factor}")
    print(f"💰 [CYCLE CHALLENGE] ${args.cycle_start} ➔ ${args.cycle_target}: {cycle_results['targets_hit']} Targets Reached, {cycle_results['busted']} Busted across {cycle_results['total_cycles']} Cycles (Win Rate: {cycle_results['cycle_win_rate_pct']}%, Net PnL: ${cycle_results['total_net_profit_usdt']} USDT)")

    # ZERO AI API Calls Mode (Fast local rule-based evaluations)
    if args.ai_provider in ("none", "offline", "disabled"):
        print("\n⚡ [AI CALLS STOPPED] Zero external AI API calls dispatched. Populating instant quantitative heuristics...")
        for t in all_trades:
            strat = t.indicators_at_entry.get("strategy", "confluence")
            is_win = t.pnl_r > 0
            t.ai_why_it_worked = f"Clean impulse move with {strat} alignment. Peak gain reached +{t.peak_gain_pct}% (+{t.peak_r}R)." if is_win else "Order flow held initial structure briefly before reversal."
            t.ai_why_it_failed = "None — target multiple was fully achieved." if is_win else f"Adverse excursion reached {t.max_drawdown_pct}% triggering {t.exit_reason}."
            t.ai_key_takeaway = f"Strategy '{strat}' executed with 0 API calls."
            t.ai_thinking_trace = f"<think>\n[Zero AI Calls Mode]\nStrategy: {strat}\nExit Reason: {t.exit_reason}\n</think>"
            t.ai_source_model = "offline_quant_engine"
        evaluated_trades = all_trades[:min(args.max_ai_reviews, 250, len(all_trades))]
    else:
        # Maximum Hugging Face AI Reasoning on Representative Sample
        evaluator = NestedThinkingEvaluator(max_concurrency=4)
        sample_size = min(args.max_ai_reviews, 250, len(all_trades))
        tp_trades = [t for t in all_trades if t.pnl_r > 0][:sample_size // 3]
        sl_trades = [t for t in all_trades if t.pnl_r < 0][:sample_size // 3]
        part_trades = [t for t in all_trades if t.target_pct_reached >= 40.0 and t.pnl_r <= 0][:sample_size // 3]
        ai_sample = tp_trades + sl_trades + part_trades

        print(f"\n🧠 [AI THINKING ENGINE] Dispatching {len(ai_sample)} trades for deep nested thinking & dual post-mortem...")
        streamed_trades = []

        def on_ai_progress(trade: SimulatedTrade, done: int, total: int):
            status["hf_calls_dispatched"] = evaluator.total_calls_dispatched
            status["hf_calls_succeeded"] = evaluator.total_calls_succeeded
            status["progress_pct"] = round(65.0 + (done / total) * 34.0, 1)
            status["elapsed_seconds"] = int(time.time() - start_time)
            formatted_trade = {
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
                "tp1_hit": trade.tp1_hit,
                "breakeven_ratchet_hit": trade.breakeven_ratchet_hit,
                "why_it_worked": trade.ai_why_it_worked,
                "why_it_failed": trade.ai_why_it_failed,
                "key_takeaway": trade.ai_key_takeaway,
                "thinking_trace": trade.ai_thinking_trace,
                "source_model": trade.ai_source_model,
            }
            streamed_trades.insert(0, formatted_trade)
            status["recent_trades"] = streamed_trades[:25]
            update_status(status)

        evaluated_trades = await evaluator.evaluate_batch(ai_sample, progress_callback=on_ai_progress)

    # Save output report
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report_data = {
        "generated_at": datetime.now(UTC).isoformat(),
        "symbols": symbols,
        "window_years": args.years,
        "total_trades": len(all_trades),
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "hf_reviews_count": len(evaluated_trades),
        "trades": [
            {
                "id": t.trade_id,
                "symbol": t.symbol,
                "direction": t.direction,
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "tp_price": t.tp_price,
                "sl_price": t.sl_price,
                "pnl_pct": t.pnl_pct,
                "pnl_r": t.pnl_r,
                "exit_reason": t.exit_reason,
                "peak_gain_pct": t.peak_gain_pct,
                "peak_r": t.peak_r,
                "target_pct_reached": t.target_pct_reached,
                "max_drawdown_pct": t.max_drawdown_pct,
                "tp1_hit": t.tp1_hit,
                "breakeven_ratchet_hit": t.breakeven_ratchet_hit,
                "why_it_worked": t.ai_why_it_worked,
                "why_it_failed": t.ai_why_it_failed,
                "key_takeaway": t.ai_key_takeaway,
                "thinking_trace": t.ai_thinking_trace,
            }
            for t in evaluated_trades
        ],
    }

    with open(REPORT_PATH, "w") as f:
        json.dump(report_data, f, indent=2)

    with open(CYCLE_REPORT_PATH, "w") as f:
        json.dump(cycle_results, f, indent=2)

    # Final status update
    status["is_running"] = False
    status["progress_pct"] = 100.0
    status["elapsed_seconds"] = int(time.time() - start_time)
    status["hf_calls_dispatched"] = evaluator.total_calls_dispatched if "evaluator" in locals() else 0
    status["hf_calls_succeeded"] = evaluator.total_calls_succeeded if "evaluator" in locals() else 0
    status["recent_trades"] = report_data["trades"][:20]
    update_status(status)

    print(f"🎉 [SIMULATOR COMPLETE] All {len(all_trades)} trades simulated and {len(evaluated_trades)} HF reviews saved to {REPORT_PATH}!")


if __name__ == "__main__":
    asyncio.run(main())
