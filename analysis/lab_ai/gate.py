"""AI as an entry gate: does a local model, shown ONLY what was knowable at entry, pick better trades?

The context drops every outcome and every post-entry field, so the model cannot see the result.
The experiment compares the same trades taken without AI (every signal) and with AI (only the ones
the model approves). Nothing here changes the simulator or its numbers.
"""
from __future__ import annotations

import json
import time

import numpy as np

from analysis.lab_ai import context
from analysis.lab_ai.ollama import OllamaError, chat, chat_think

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["decision", "score", "reason"],
          "properties": {"decision": {"type": "string", "enum": ["take", "skip"]},
                         "score": {"type": "integer", "minimum": 1, "maximum": 5},
                         "reason": {"type": "string"}}}

SYSTEM = (
    "A rule-based crypto strategy produced this entry signal. You see ONLY what was known at the moment of "
    "entry; the result is hidden. Decide whether to TAKE or SKIP it.\n\n"
    "The strategy's own rule is already satisfied, so do not re-judge that. Judge the setting around it using "
    "the numbers: how stretched price is in its 24h range (pos_in_24h_range_pct), volume against normal "
    "(volume_1h_vs_avg), volatility (atr_15m_pct) against the stop (stop_pct) and the round-trip cost, the "
    "recent trend (ret_1h/4h/24h, trend_4h, btc_24h_ret_pct) relative to the trade's side, and the shape of the "
    "last 16 closes.\n\n"
    "score 1-5 = how likely this trade reaches its target before its stop (1 very unlikely, 5 very likely). "
    "Entries like this lose roughly half the time, so SKIP only when the numbers give a clear reason, and say "
    "which numbers. reason: at most 30 words and it must quote at least two numbers from the data.\n"
    "Never invent news. If NEWS lines are given, use them only where they bear on this coin at this time. "
    "Reply with the JSON object only.")

HIDDEN = ("outcome", "next_16_closes_15m_pct_in_trade_direction")
# Models were trained on this history: shown "SOL, 2022-11-08" one can recall the FTX crash instead of reasoning.
# The coin and the date are hidden so a decision rests on the numbers only (design/7d_forecast_plan.md, leakage).
IDENTIFYING = ("symbol", "entry_utc")


def pretrade_context(trade, root: str = "data/lake", anonymize: bool = True) -> dict:
    ctx = context.build(trade, root, extended=True)
    for k in HIDDEN + (IDENTIFYING if anonymize else ()):
        ctx.pop(k, None)
    if anonymize:
        ctx = {"coin": "Coin A", **ctx}
    return ctx


def features(ctx: dict, side: int) -> dict:
    """Flat numeric features known at entry (for a plain non-AI filter). Returns are signed by trade direction."""
    a = ctx.get("at_entry", {})
    t = ctx.get("trend_4h", {})
    f = {"ret_1h": a.get("ret_1h_pct", 0) * side, "ret_4h": a.get("ret_4h_pct", 0) * side,
         "ret_24h": a.get("ret_24h_pct", 0) * side, "rsi": a.get("rsi14_15m", 50) - 50,
         "atr": a.get("atr_15m_pct", 0), "range_pos": (a.get("pos_in_24h_range_pct", 50) - 50) * side / 50,
         "vol": min(a.get("volume_1h_vs_avg", 1), 10), "btc_24h": a.get("btc_24h_ret_pct", 0) * side,
         "ema50_dist": t.get("close_vs_ema50_pct", 0) * side, "stop_pct": ctx.get("stop_pct", 0), "side": side}
    return {k: float(v) for k, v in f.items()}


def prompt(ctx: dict, news=None) -> str:
    text = "ENTRY SIGNAL\n" + json.dumps(ctx, indent=1)
    if news:
        text += "\n\nNEWS (published before entry):\n" + "\n".join(f"- {n}" for n in news[:6])
    return text


def _json_from(text):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no json")
    return json.loads(text[a:b + 1])


def decide(ctx: dict, model: str, think: bool = True, think_tokens: int = 2000, news=None, num_ctx: int = 8192,
           host: str = "http://127.0.0.1:11434") -> dict:
    """Bounded think-then-answer, like review_x, so a thinking model always ends with a decision."""
    t0 = time.time()
    user = prompt(ctx, news)
    out_tok, think_chars, data, last = 0, 0, None, {}
    try:
        if think:
            r1 = model.startswith("deepseek-r1")
            a = chat_think(model, SYSTEM, user, schema=None, num_predict=int(think_tokens * (1.5 if r1 else 1)), num_ctx=num_ctx, host=host)
            out_tok, think_chars, last = a["out_tokens"], len(a["thinking"]), a
            try:
                data = _json_from(a["text"])
                if data.get("decision") not in ("take", "skip"):
                    data = None
            except (ValueError, json.JSONDecodeError):
                data = None
            if data is None:
                notes = (a["thinking"] or a["text"])[-1800:]
                if r1:      # deepseek-r1 cannot stop thinking: give it room and ask for the JSON in plain text
                    follow = user + "\n\nYour reasoning so far (may be cut off):\n" + notes + "\n\nStop reasoning. Output ONLY the final JSON object now."
                    b = chat(model, SYSTEM, follow, schema=None, think=None, num_predict=500, temperature=0.3,
                             num_ctx=num_ctx, host=host)
                else:
                    follow = user + "\n\nYour analysis so far (may be cut off):\n" + notes + "\n\nNow give the final JSON only."
                    try:
                        b = chat(model, SYSTEM, follow, schema=SCHEMA, think=False, num_predict=200, temperature=0.1,
                                 num_ctx=num_ctx, host=host)
                    except OllamaError:
                        b = chat(model, SYSTEM, follow, schema=SCHEMA, think=None, num_predict=200, temperature=0.1,
                                 num_ctx=num_ctx, host=host)
                out_tok += b["out_tokens"]
                last = b
                data = _json_from(b["text"])
        else:
            last = chat(model, SYSTEM, user, schema=SCHEMA, think=False, num_predict=250, temperature=0.2,
                        num_ctx=num_ctx, host=host)
            out_tok = last["out_tokens"]
            data = _json_from(last["text"])
        if data.get("decision") not in ("take", "skip") or not isinstance(data.get("score"), int):
            raise ValueError("invalid")
    except (OllamaError, ValueError, json.JSONDecodeError) as e:
        return {"ok": False, "error": str(e)[:100], "model": model, "wall_s": time.time() - t0}
    return {"ok": True, "model": model, "decision": data["decision"], "score": int(data["score"]),
            "reason": str(data.get("reason", ""))[:240], "wall_s": time.time() - t0, "out_tokens": out_tok,
            "thinking_chars": think_chars, "tok_per_s": last.get("tok_per_s", 0.0)}


def compare(rows: list[dict]) -> dict:
    """Without AI (every signal) vs with AI (approved only), on the trades reviewed so far."""
    ok = [r for r in rows if r.get("ok")]
    if len(ok) < 4:
        return {"n": len(ok)}
    r = np.array([x["r_net"] for x in ok])
    take = np.array([x["decision"] == "take" for x in ok])
    score = np.array([x["score"] for x in ok], float)
    win = r > 0

    def pf(v):
        g, l = v[v > 0].sum(), -v[v < 0].sum()
        return float(g / l) if l > 0 else None

    out = {"n": len(ok), "take_rate": float(take.mean()),
           "all_exp_r": float(r.mean()), "all_win": float(win.mean()), "all_pf": pf(r),
           "all_total_r": float(r.sum()), "all_return_pct_at_1pct_risk": float(r.sum())}
    if take.any():
        out.update(take_n=int(take.sum()), take_exp_r=float(r[take].mean()), take_win=float(win[take].mean()), take_pf=pf(r[take]),
                   take_total_r=float(r[take].sum()), take_return_pct_at_1pct_risk=float(r[take].sum()),
                   lift_r_per_trade=float(r[take].mean() - r.mean()))
    if (~take).any():
        out.update(skip_n=int((~take).sum()), skip_exp_r=float(r[~take].mean()), skip_win=float(win[~take].mean()))
    if score.std() > 0 and r.std() > 0:
        from scipy.stats import spearmanr
        out["spearman_score_vs_r"] = float(spearmanr(score, r)[0])
    if win.any() and (~win).any() and score.std() > 0:
        pos, neg = score[win], score[~win]
        out["auc"] = float(np.mean([(p > q) + 0.5 * (p == q) for p in pos for q in neg]))
    if take.any() and (~take).any():
        rng = np.random.default_rng(1)
        d = [r[rng.choice(np.flatnonzero(take), take.sum())].mean() - r[rng.choice(np.flatnonzero(~take), (~take).sum())].mean()
             for _ in range(1000)]
        out["take_minus_skip_ci"] = [float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))]
    return out
