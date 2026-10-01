"""
Signal trade path evaluator & Hugging Face dual AI reasoning engine.

Evaluates trade trajectories for fired signals:
- Maximum Favorable Excursion (MFE / peak gain in % and R)
- Percentage of target distance reached before pullback/stop
- Maximum Adverse Excursion (MAE / drawdown)
- Milestones: TP1 partial hit, Breakeven ratchet reached
- Classification: 'won' (full target), 'partial' (substantial profit before reversal),
  'stopped' (direct stop loss), 'running' (currently active), 'expired' (timed out).
- Hugging Face AI dual reasoning:
  - 'why_it_worked': Quantitative momentum/volume factors that drove the trade favorably.
  - 'why_it_failed': Counter-forces, resistance, or exhaustion that blocked full target.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timezone
from typing import Any

log = logging.getLogger("trade_evaluator")

# In-memory cache for signal trade evaluations and AI dual reasoning: signal_id -> dict
_TRADE_EVAL_CACHE: dict[int, dict[str, Any]] = {}
_AI_REASONING_CACHE: dict[int, dict[str, Any]] = {}


def evaluate_signal_trade_path(
    sig: Any,
    candles: list[Any],
    current_price: float | None = None,
) -> dict[str, Any]:
    """
    Rigorously evaluate a signal's price path from entry to exit or current candle.

    Works on CryptoSignalLog model instances, dataclasses, or plain dicts.
    """
    sig_id = getattr(sig, "id", None) or (sig.get("id") if isinstance(sig, dict) else 0)
    direction = (getattr(sig, "direction", None) or (sig.get("direction") if isinstance(sig, dict) else "long")).lower()
    long_ = direction == "long"

    entry = float(getattr(sig, "current_price", 0.0) or (sig.get("current_price", 0.0) if isinstance(sig, dict) else 0.0))
    tp = float(getattr(sig, "target_price", 0.0) or (sig.get("target_price", 0.0) if isinstance(sig, dict) else 0.0))
    sl = float(getattr(sig, "stop_loss", 0.0) or (sig.get("stop_loss", 0.0) if isinstance(sig, dict) else 0.0))
    db_outcome = (getattr(sig, "outcome", None) or (sig.get("outcome") if isinstance(sig, dict) else "pending")).lower()
    db_pnl = float(getattr(sig, "pnl_pct", 0.0) or (sig.get("pnl_pct", 0.0) if isinstance(sig, dict) else 0.0))

    if entry <= 0:
        return {
            "status": db_outcome,
            "worked": db_outcome == "won",
            "worked_desc": f"Outcome: {db_outcome}",
            "target_pct_reached": 100.0 if db_outcome == "won" else 0.0,
            "peak_gain_pct": max(0.0, db_pnl),
            "peak_r": max(0.0, db_pnl / 1.0),
            "peak_price": entry,
            "max_drawdown_pct": min(0.0, db_pnl),
            "current_pnl_pct": db_pnl,
            "current_price": current_price or entry,
            "target_dist_pct": 0.0,
            "risk_dist_pct": 0.0,
            "tp1_hit": db_outcome == "won",
            "breakeven_hit": db_outcome == "won",
            "hit_target": db_outcome == "won",
            "hit_stop": db_outcome == "lost",
        }

    target_dist_pct = abs(tp - entry) / entry * 100.0 if tp > 0 else 0.0
    risk_dist_pct = abs(entry - sl) / entry * 100.0 if sl > 0 else 0.0

    hit_target = False
    hit_stop = False
    peak_price = entry
    peak_gain_pct = 0.0
    max_drawdown_pct = 0.0
    latest_price = current_price or entry

    # Filter candles strictly after signal timestamp if timestamp exists
    sig_ts = getattr(sig, "timestamp", None) or (sig.get("timestamp") if isinstance(sig, dict) else None)
    if isinstance(sig_ts, str):
        try:
            sig_ts = datetime.fromisoformat(sig_ts.replace("Z", "+00:00"))
        except Exception:
            sig_ts = None
    if sig_ts is not None and getattr(sig_ts, "tzinfo", None) is not None:
        sig_ts = sig_ts.astimezone(timezone.utc).replace(tzinfo=None)

    valid_candles = []
    for c in candles:
        c_ts = getattr(c, "timestamp", None) or (c.get("timestamp") if isinstance(c, dict) else None)
        if isinstance(c_ts, (int, float)):
            # Millisecond timestamp
            c_ts = datetime.fromtimestamp(c_ts / 1000.0, tz=timezone.utc).replace(tzinfo=None)
        elif isinstance(c_ts, str):
            try:
                c_ts = datetime.fromisoformat(c_ts.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                pass
        elif hasattr(c_ts, "tzinfo") and c_ts.tzinfo is not None:
            c_ts = c_ts.astimezone(timezone.utc).replace(tzinfo=None)

        if sig_ts and c_ts and c_ts < sig_ts:
            continue
        valid_candles.append(c)

    # Walk candles chronologically
    for c in valid_candles:
        c_high = float(getattr(c, "high", None) or (c.get("high") if isinstance(c, dict) else 0.0) or 0.0)
        c_low = float(getattr(c, "low", None) or (c.get("low") if isinstance(c, dict) else 0.0) or 0.0)
        c_close = float(getattr(c, "close", None) or (c.get("close") if isinstance(c, dict) else 0.0) or 0.0)
        if c_high <= 0 or c_low <= 0:
            continue

        latest_price = c_close

        if long_:
            gain = (c_high - entry) / entry * 100.0
            dd = (c_low - entry) / entry * 100.0
            if gain > peak_gain_pct:
                peak_gain_pct = gain
                peak_price = c_high
            if dd < max_drawdown_pct:
                max_drawdown_pct = dd

            if tp > 0 and c_high >= tp:
                hit_target = True
                peak_gain_pct = max(peak_gain_pct, target_dist_pct)
                peak_price = max(peak_price, tp)
                break
            if sl > 0 and c_low <= sl:
                hit_stop = True
                break
        else:  # short
            gain = (entry - c_low) / entry * 100.0
            dd = (entry - c_high) / entry * 100.0
            if gain > peak_gain_pct:
                peak_gain_pct = gain
                peak_price = c_low
            if dd < max_drawdown_pct:
                max_drawdown_pct = dd

            if tp > 0 and c_low <= tp:
                hit_target = True
                peak_gain_pct = max(peak_gain_pct, target_dist_pct)
                peak_price = min(peak_price, tp)
                break
            if sl > 0 and c_high >= sl:
                hit_stop = True
                break

    # If DB already resolved this signal, respect it
    if db_outcome == "won":
        hit_target = True
        hit_stop = False
        peak_gain_pct = max(peak_gain_pct, target_dist_pct, abs(db_pnl))
        peak_price = tp if tp > 0 else peak_price
    elif db_outcome == "lost" and not hit_target:
        hit_stop = True

    # Excursion metrics
    target_pct_reached = (
        min(100.0, max(0.0, (peak_gain_pct / target_dist_pct * 100.0)))
        if target_dist_pct > 0
        else (100.0 if hit_target else 0.0)
    )
    peak_r = round(peak_gain_pct / risk_dist_pct, 2) if risk_dist_pct > 0 else 0.0
    tp1_hit = target_pct_reached >= 50.0 or peak_gain_pct >= 0.50
    breakeven_hit = peak_r >= 1.0 or peak_gain_pct >= 0.80

    # Current PnL
    if long_:
        current_pnl_pct = (latest_price - entry) / entry * 100.0
    else:
        current_pnl_pct = (entry - latest_price) / entry * 100.0

    # Determine status & narrative
    if hit_target:
        status = "won"
        worked = True
        worked_desc = f"🏆 Worked 100%: Hit full take-profit target (+{target_dist_pct:.2f}%, +{peak_r:.2f}R)"
        realized_pnl = target_dist_pct
    elif hit_stop:
        realized_pnl = -risk_dist_pct
        if target_pct_reached >= 40.0 or peak_gain_pct >= 0.40:
            status = "partial"
            worked = True
            worked_desc = (
                f"⚡ Worked Partially: Peaked at +{peak_gain_pct:.2f}% (+{peak_r:.2f}R), "
                f"covering {target_pct_reached:.1f}% of target before reversing to stop"
            )
        else:
            status = "stopped"
            worked = False
            worked_desc = (
                f"🛑 Direct Stop Out: Reached only {target_pct_reached:.1f}% of target (+{peak_gain_pct:.2f}%), "
                f"stopped at -{risk_dist_pct:.2f}%"
            )
    else:
        # Trade is currently active / running
        status = "running"
        realized_pnl = current_pnl_pct
        worked = target_pct_reached >= 40.0 or current_pnl_pct > 0
        worked_desc = (
            f"🔵 Live Trade Active: Currently {current_pnl_pct:+.2f}%, "
            f"peaked at +{peak_gain_pct:.2f}% ({target_pct_reached:.1f}% to target)"
        )

    res = {
        "status": status,
        "worked": worked,
        "worked_desc": worked_desc,
        "target_pct_reached": round(target_pct_reached, 1),
        "peak_gain_pct": round(peak_gain_pct, 2),
        "peak_r": peak_r,
        "peak_price": round(peak_price, 4),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "current_pnl_pct": round(current_pnl_pct, 2),
        "current_price": round(latest_price, 4),
        "realized_pnl_pct": round(realized_pnl, 2),
        "target_dist_pct": round(target_dist_pct, 2),
        "risk_dist_pct": round(risk_dist_pct, 2),
        "tp1_hit": tp1_hit,
        "breakeven_hit": breakeven_hit,
        "hit_target": hit_target,
        "hit_stop": hit_stop,
    }

    if sig_id and status in ("won", "stopped", "partial"):
        _TRADE_EVAL_CACHE[sig_id] = res

    return res


async def get_mirror_ai_dual_reasoning(
    sig: Any,
    trade_check: dict[str, Any],
) -> dict[str, str]:
    """
    Generate Hugging Face AI dual reasoning:
    1. 'why_it_worked': what momentum/volume/orderflow factor drove favorable movement.
    2. 'why_it_failed': what counter-force, resistance, or exhaustion caused pullback/stop.
    3. 'key_takeaway': actionable heuristic.

    Uses collectors.llm_client.ask_json targeting the configured model chain (Hugging Face primary).
    Caches results by signal ID so each trade is analyzed once.
    """
    from collectors.llm_client import ask_json

    sig_id = getattr(sig, "id", None) or (sig.get("id") if isinstance(sig, dict) else 0)
    if sig_id and sig_id in _AI_REASONING_CACHE:
        return _AI_REASONING_CACHE[sig_id]

    sym = (getattr(sig, "symbol", "") or (sig.get("symbol", "") if isinstance(sig, dict) else "")).upper()
    direction = (getattr(sig, "direction", "") or (sig.get("direction", "") if isinstance(sig, dict) else "")).upper()
    entry = getattr(sig, "current_price", 0.0) or (sig.get("current_price", 0.0) if isinstance(sig, dict) else 0.0)
    tp = getattr(sig, "target_price", 0.0) or (sig.get("target_price", 0.0) if isinstance(sig, dict) else 0.0)
    sl = getattr(sig, "stop_loss", 0.0) or (sig.get("stop_loss", 0.0) if isinstance(sig, dict) else 0.0)
    trigger = getattr(sig, "trigger_description", "") or (sig.get("trigger", "") if isinstance(sig, dict) else "")
    indicators = getattr(sig, "indicators_summary", "") or (sig.get("indicators", "") if isinstance(sig, dict) else "")

    status = trade_check.get("status", "running")
    peak_gain = trade_check.get("peak_gain_pct", 0.0)
    peak_r = trade_check.get("peak_r", 0.0)
    peak_p = trade_check.get("peak_price", entry)
    tgt_reached = trade_check.get("target_pct_reached", 0.0)
    dd = trade_check.get("max_drawdown_pct", 0.0)
    curr_pnl = trade_check.get("current_pnl_pct", 0.0)

    system_prompt = (
        "You are an institutional quantitative cryptocurrency execution analyst. "
        "Analyze this systematic Mirror Trade setup and its actual market price trajectory. "
        "Provide a dual post-mortem breakdown:\n"
        "1. WHY IT WORKED: Explain precisely what price action, indicator confluence, "
        "or volume dynamics drove favorable movement to the peak price.\n"
        "2. WHY IT FAILED / RETRACED: Explain what counter-trend resistance, buyer/seller absorption, "
        "liquidity sweep, or exhaustion blocked full target completion or triggered the stop.\n"
        "3. KEY TAKEAWAY: 1 concise quantitative heuristic for future trade management.\n\n"
        "Respond STRICTLY in JSON format with keys: 'why_it_worked', 'why_it_failed', 'key_takeaway'."
    )

    user_prompt = (
        f"Trade: {sym} {direction}\n"
        f"Entry: {entry} | TP: {tp} | SL: {sl}\n"
        f"Setup Signal: {trigger}\n"
        f"Indicators: {indicators}\n"
        f"Actual Performance:\n"
        f"- Status: {status.upper()}\n"
        f"- Peak Favorable Price: {peak_p} (+{peak_gain}% / +{peak_r}R)\n"
        f"- Target Achieved: {tgt_reached}%\n"
        f"- Max Adverse Drawdown: {dd}%\n"
        f"- Final / Current PnL: {curr_pnl}%\n"
    )

    try:
        reply = await ask_json(
            role="attribution",
            system=system_prompt,
            user=user_prompt,
            max_tokens=450,
            temperature=0.1,
            timeout=18.0,
        )
        if reply and isinstance(reply.data, dict) and reply.data.get("why_it_worked"):
            reasoning = {
                "why_it_worked": str(reply.data.get("why_it_worked", "")),
                "why_it_failed": str(reply.data.get("why_it_failed", "")),
                "key_takeaway": str(reply.data.get("key_takeaway", "")),
                "source_model": reply.provider_model or "huggingface",
            }
            if sig_id:
                _AI_REASONING_CACHE[sig_id] = reasoning
            return reasoning
    except Exception as exc:
        log.warning("mirror_ai_reasoning_call_failed", error=str(exc))

    # Algorithmic fallback if all LLMs are unreachable
    if status == "won":
        w_work = f"Strong {direction.lower()} momentum expanded cleanly past entry {entry}, confirming confluence and sweeping past target {tp} without triggering trailing stop."
        w_fail = "Minimal resistance encountered; minor opposing tick pullbacks were completely absorbed by trend volume."
        takeaway = "Clean continuation setup; full target reached with favorable risk-reward expansion."
    elif status == "partial":
        w_work = f"Initial {direction.lower()} impulse gained +{peak_gain:.2f}% (+{peak_r:.2f}R), covering {tgt_reached:.0f}% of the target distance due to sharp short-term momentum."
        w_fail = f"Momentum exhausted near {peak_p} as opposing liquidity stepped in; lack of follow-through volume caused a full reversal into stop loss {sl}."
        takeaway = "Lock partial profits (TP1) or ratchet stop to breakeven once price covers >= 50% of the target distance to prevent giving back gains."
    elif status == "stopped":
        w_work = f"Brief favorable tick of +{peak_gain:.2f}% before counter-trend pressure overwhelmed entry."
        w_fail = f"Opposing orderflow dominated immediately; price broke through stop loss {sl} (-{abs(dd):.2f}%) due to strong counter-trend trend divergence."
        takeaway = "Respect stop loss unconditionally; opposite direction signal had higher structural dominance."
    else:
        w_work = f"Currently moving favorably at {curr_pnl:+.2f}%, reaching up to {tgt_reached:.0f}% of target distance."
        w_fail = f"Opposing limit orders capped momentum at {peak_p}; trade remains active within volatility bands."
        takeaway = "Monitor 15m volume confirmation as price approaches TP1 threshold."

    fallback = {
        "why_it_worked": w_work,
        "why_it_failed": w_fail,
        "key_takeaway": takeaway,
        "source_model": "algorithmic_synthesis",
    }
    if sig_id and status in ("won", "stopped", "partial"):
        _AI_REASONING_CACHE[sig_id] = fallback
    return fallback
