"""
GET /api/simulator/preview: what the intraday simulator's settings mean, before a run.

The Book › Intraday sim page renders this instead of doing the maths in the browser (owner, 2 Nov 2026: "core logic
should be in backend, very less exposure at frontend"). It is the same arithmetic the classic simulator form did
client-side, unchanged:

- run size: market hours in the chosen period and how many steps that is at the chosen step size
- one trade's money: margin = cycle start × margin %, position = margin × leverage, and what the target and the stop
  are worth at that leverage. 1R is shown as an illustrative 1.2 % price move (the engine sizes 1R from ATR, so the
  real move varies per trade); the page says so.

Also the form's choices (strategies, periods, AI providers) and defaults, so the page ships no lists of its own.
Pure function, no I/O: safe on a public route.
"""

from __future__ import annotations

ONE_R_PRICE_PCT = 1.2           # illustrative; the engine's 1R comes from ATR

STRATEGIES = (
    ("all", "All strategies together"),
    ("confluence", "5-family confluence gate"),
    ("bollinger_squeeze", "Bollinger squeeze breakout"),
    ("volume_spike", "Volume spike"),
    ("rsi_divergence", "RSI divergence (reversals)"),
    ("trend_pullback", "Trend pullback and continuation"),
    ("range_breakout", "Range breakout"),
    ("sweep_reclaim", "Liquidity sweep and reclaim"),
    ("breakout_retest", "Breakout and retest"),
)
PERIODS = (
    ("0.00274", "1 day"), ("0.0082", "3 days"), ("0.0192", "1 week"), ("0.0384", "2 weeks"),
    ("0.083", "1 month"), ("0.25", "3 months"), ("1.0", "1 year"), ("3.0", "3 years"),
)
AI_PROVIDERS = (
    ("none", "Off (no AI calls, fastest, free)"),
    ("auto", "Auto (Gemini, Groq, OpenRouter, Hugging Face)"),
    ("gemini", "Gemini only"),
    ("groq", "Groq only"),
    ("openrouter", "OpenRouter free"),
    ("hf", "Hugging Face free"),
)
DEFAULTS = {
    "strategy": "all", "years": "0.0082", "tp_r": 3.0, "sl_r": 1.0, "trail_sl": True, "anti_flip": 90,
    "stagnation": 60, "cycle_start": 25.0, "cycle_target": 100.0, "cycle_margin_pct": 25.0, "cycle_leverage": 10.0,
    "stepped_mode": True, "step_market_hours": 1.0, "ai_provider": "none",
}
_BOUNDS = {                      # (min, max) so a typo cannot produce absurd numbers on the page
    "tp_r": (0.1, 50.0), "sl_r": (0.1, 20.0), "cycle_start": (1.0, 1e7), "cycle_target": (1.0, 1e8),
    "cycle_margin_pct": (0.1, 100.0), "cycle_leverage": (1.0, 125.0), "step_market_hours": (0.1, 24.0),
    "years": (0.001, 5.0),
}


def _num(raw, key: str) -> float:
    lo, hi = _BOUNDS[key]
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = float(DEFAULTS[key])
    if v != v:                   # NaN
        v = float(DEFAULTS[key])
    return max(lo, min(hi, v))


def preview(params: dict) -> dict:
    """Options, defaults and the derived numbers for one set of form values (strings or numbers)."""
    p = {k: params.get(k, v) for k, v in DEFAULTS.items()}
    years = _num(p["years"], "years")
    step_h = _num(p["step_market_hours"], "step_market_hours")
    tp_r, sl_r = _num(p["tp_r"], "tp_r"), _num(p["sl_r"], "sl_r")
    start, target = _num(p["cycle_start"], "cycle_start"), _num(p["cycle_target"], "cycle_target")
    margin_pct, lev = _num(p["cycle_margin_pct"], "cycle_margin_pct"), _num(p["cycle_leverage"], "cycle_leverage")

    hours = round(years * 8760)
    margin = max(0.01, start * margin_pct / 100.0)
    tp_price, sl_price = tp_r * ONE_R_PRICE_PCT, sl_r * ONE_R_PRICE_PCT
    tp_roe, sl_roe = tp_price * lev, sl_price * lev
    return {
        "options": {
            "strategies": [{"value": v, "label": t} for v, t in STRATEGIES],
            "periods": [{"value": v, "label": t} for v, t in PERIODS],
            "ai_providers": [{"value": v, "label": t} for v, t in AI_PROVIDERS],
        },
        "defaults": DEFAULTS,
        "run": {"market_hours": hours, "steps": max(1, round(hours / step_h))},
        "cycle": {"start_usdt": start, "target_usdt": target, "margin_pct": margin_pct, "leverage": lev,
                  "rule": f"Margin {margin_pct:g}% of the wallet · {lev:g}x · every fee charged"},
        "trade": {
            "margin_usdt": round(margin, 2),
            "position_usdt": round(margin * lev, 2),
            "one_r_price_pct": ONE_R_PRICE_PCT,
            "target_price_pct": round(tp_price, 2), "stop_price_pct": round(sl_price, 2),
            "target_roe_pct": round(tp_roe, 1), "stop_roe_pct": round(sl_roe, 1),
            "target_cash_usdt": round(tp_roe / 100.0 * margin, 2), "stop_cash_usdt": round(sl_roe / 100.0 * margin, 2),
            "reward_risk": round(tp_r / sl_r, 2),
        },
        "note": f"1R is shown as a {ONE_R_PRICE_PCT}% price move for illustration; the engine sizes 1R from each "
                "coin's ATR, so real trades differ.",
    }
