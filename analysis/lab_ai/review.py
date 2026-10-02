"""Prompt, schema and runner for reviewing finished trades with a local model."""
from __future__ import annotations

import json
import time

from analysis.lab_ai.ollama import OllamaError, chat, chat_think

CAUSES = ["clean_win", "trend_follow_through", "stopped_by_noise", "cost_drag", "regime_mismatch",
          "news_shock", "false_signal", "time_decay", "other"]

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["cause", "signal_quality", "evidence", "check_next"],
          "properties": {"cause": {"type": "string", "enum": CAUSES},
                         "signal_quality": {"type": "integer", "minimum": 1, "maximum": 5},
                         "evidence": {"type": "string"}, "check_next": {"type": "string"}}}

SYSTEM = (
    "You review ONE finished backtest trade from a crypto strategy test. The numbers are computed from "
    "real candles; trust them and do not recompute. Decide why the trade ended the way it did.\n\n"
    "Rules:\n"
    "- cause: pick exactly one of the allowed values. cost_drag means the gross result was positive or near "
    "zero but fees took it negative. stopped_by_noise means MAE reached the stop but the move later went the "
    "trade's way (see move_after_60m_r).\n"
    "- evidence: at most 40 words and it MUST quote at least two numbers from the data.\n"
    "- check_next: one thing a human could test across MANY trades (a filter or a condition). Never advise a "
    "new stop, target or size from this single trade.\n"
    "- signal_quality 1-5: was the entry condition sensible given the numbers at entry, independent of luck?\n"
    "Never invent news. If NEWS lines are given, use them only if they plausibly bear on this coin and time.\n"
    "Reply with JSON only."
)


def prompt(ctx: dict, news: list[str] | None = None) -> str:
    text = "TRADE\n" + json.dumps(ctx, indent=1)
    if news:
        text += "\n\nNEWS (published before entry):\n" + "\n".join(f"- {n}" for n in news[:6])
    return text


def review(ctx: dict, model: str, think: bool | None = None, num_predict: int = 300, news=None,
           host: str = "http://127.0.0.1:11434") -> dict:
    t0 = time.time()
    try:
        r = chat(model, SYSTEM, prompt(ctx, news), schema=SCHEMA, think=think, num_predict=num_predict, host=host)
    except OllamaError as e:
        return {"ok": False, "error": str(e), "model": model, "wall_s": time.time() - t0}
    try:
        data = json.loads(r["text"])
        ok = data.get("cause") in CAUSES and isinstance(data.get("signal_quality"), int)
    except (json.JSONDecodeError, TypeError):
        data, ok = {}, False
    return {"ok": ok, "model": model, "think": think, **{k: data.get(k) for k in ("cause", "signal_quality", "evidence", "check_next")},
            "wall_s": r["wall_s"], "out_tokens": r["out_tokens"], "prompt_tokens": r["prompt_tokens"],
            "tok_per_s": r["tok_per_s"], "thinking_chars": len(r["thinking"]), "raw": r["text"][:300] if not ok else ""}


# ---- extreme configuration: deeper reasoning, a testable hypothesis, a confidence --------
SCHEMA_X = {"type": "object", "additionalProperties": False,
            "required": ["cause", "signal_quality", "evidence", "hypothesis", "confidence"],
            "properties": {"cause": {"type": "string", "enum": CAUSES},
                           "signal_quality": {"type": "integer", "minimum": 1, "maximum": 5},
                           "evidence": {"type": "string"}, "hypothesis": {"type": "string"},
                           "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}

CAUSE_DEFS = {
    "clean_win": "r_net > 0.5",
    "trend_follow_through": "a win where mfe_r >= 2 and the next closes kept moving in the trade direction",
    "stopped_by_noise": "exit_reason is stop, but move_after_60m_r > 0.5 (price came back the trade's way)",
    "false_signal": "exit_reason is stop and move_after_60m_r < -0.5 and mfe_r < 0.5 (it never worked)",
    "cost_drag": "r_gross > 0 but r_net < 0 (fees and funding turned it into a loss)",
    "time_decay": "exit_reason is time and abs(r_net) < 0.3 (nothing happened)",
    "regime_mismatch": "a loss taken against the 4h trend or against btc_24h_ret_pct",
    "news_shock": "only when a NEWS line plausibly explains a sudden move",
    "other": "none of the above fits",
}


def system_x(key: str = "") -> str:
    """The reviewer prompt with the cause list shuffled per trade, so a small model cannot just
    pick whichever cause is listed first."""
    import random
    order = list(CAUSE_DEFS)
    random.Random(key).shuffle(order)
    defs = "\n".join(f"  {c}: {CAUSE_DEFS[c]}" for c in order)
    return (
        "You review ONE finished backtest trade from a crypto strategy test. The numbers are computed from "
        "real candles; trust them and do not recompute.\n\n"
        "Pick the ONE cause whose rule the numbers satisfy. Check the rules against the data, one by one, "
        "before answering. The causes, in no particular order:\n" + defs + "\n\n"
        "Then:\n"
        "- evidence: at most 40 words, quoting at least two numbers from the data.\n"
        "- signal_quality 1-5: was the ENTRY sensible given at_entry numbers, independent of luck?\n"
        "- hypothesis: one filter a program could test over thousands of trades. It may use ONLY fields known "
        "before the entry: at_entry, trend_4h, last_16_closes_15m_pct_vs_entry, stop_pct, side, strategy. "
        "Never use anything under outcome (r_net, mae_r, mfe_r, move_after_*). Example: 'skip longs when "
        "pos_in_24h_range_pct > 90 and volume_1h_vs_avg < 1'. Never advise a new stop, target or size.\n"
        "- confidence 0-1: lower it when two causes both fit.\n"
        "Never invent news. Reply with the JSON object only.")


def _json_from(text: str):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no json")
    return json.loads(text[a:b + 1])


def review_x(ctx: dict, model: str, think: bool | None, num_predict: int = 700, news=None, num_ctx: int = 8192,
             host: str = "http://127.0.0.1:11434", key: str = "") -> dict:
    """Extreme-config review. A thinking model gets a bounded thinking pass, then a short constrained
    answer pass, so it can never run out of tokens before it writes the JSON."""
    t0 = time.time()
    system, user = system_x(key), prompt(ctx, news)
    data, err, last = None, None, {}
    tok = {"out": 0, "think_chars": 0}
    try:
        if think:
            r1 = model.startswith("deepseek-r1")
            a = chat_think(model, system, user, schema=None, num_predict=num_predict * (2 if r1 else 1), num_ctx=num_ctx, host=host)
            tok["out"] += a["out_tokens"]
            tok["think_chars"] = len(a["thinking"])
            try:
                data = _json_from(a["text"])
                if data.get("cause") not in CAUSES:
                    data = None
            except (ValueError, json.JSONDecodeError):
                data = None
            last = a
            if data is None:
                notes = (a["thinking"] or a["text"])[-1800:]
                if r1:
                    follow = user + "\n\nYour reasoning so far (may be cut off):\n" + notes + "\n\nStop reasoning. Output ONLY the final JSON object now."
                    b = chat(model, system, follow, schema=None, think=None, num_predict=600, temperature=0.3,
                             num_ctx=num_ctx, host=host)
                else:
                    follow = user + "\n\nYour analysis so far (may be cut off):\n" + notes + "\n\nNow give the final JSON object only."
                    try:
                        b = chat(model, system, follow, schema=SCHEMA_X, think=False, num_predict=260, temperature=0.1,
                                 num_ctx=num_ctx, host=host)
                    except OllamaError:
                        b = chat(model, system, follow, schema=SCHEMA_X, think=None, num_predict=260, temperature=0.1,
                                 num_ctx=num_ctx, host=host)
                tok["out"] += b["out_tokens"]
                last = b
                data = _json_from(b["text"])
        else:
            last = chat(model, system, user, schema=SCHEMA_X, think=think, num_predict=num_predict, temperature=0.2,
                        num_ctx=num_ctx, host=host)
            tok["out"] += last["out_tokens"]
            data = _json_from(last["text"])
        if data.get("cause") not in CAUSES:
            raise ValueError("invalid cause")
    except (OllamaError, ValueError, json.JSONDecodeError) as e:
        return {"ok": False, "error": str(e)[:120], "model": model, "think": think, "wall_s": time.time() - t0}
    return {"ok": True, "model": model, "think": think,
            **{k: data.get(k) for k in ("cause", "signal_quality", "evidence", "hypothesis", "confidence")},
            "wall_s": time.time() - t0, "out_tokens": tok["out"], "prompt_tokens": last.get("prompt_tokens", 0),
            "tok_per_s": last.get("tok_per_s", 0.0), "thinking_chars": tok["think_chars"]}


# ---- grading against facts the code can check ------------------------------------------
import re


def oracle_causes(ctx: dict) -> set[str]:
    """Causes that the numbers themselves support. Empty when the numbers settle nothing."""
    o = ctx["outcome"]
    ok = set()
    if o["r_net"] > 0.5:
        ok.add("clean_win")
    if o["r_gross"] > 0.05 and o["r_net"] < 0:
        ok.add("cost_drag")
    if o["exit_reason"] == "stop" and o["move_after_60m_r"] > 0.5:
        ok.add("stopped_by_noise")
    if o["exit_reason"] == "stop" and o["move_after_60m_r"] < -0.5 and o["mfe_r"] < 0.5:
        ok.add("false_signal")
    if o["exit_reason"] == "time" and abs(o["r_net"]) < 0.3:
        ok.add("time_decay")
    return ok


def _numbers(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        for v in obj.values():
            _numbers(v, out)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append(abs(float(obj)))
    return out


def grade(ctx: dict, result: dict) -> dict:
    """consistent: does the label agree with what the numbers support (None when they settle nothing).
    cited/grounded: numbers quoted in the evidence, and how many really appear in the data."""
    truth = oracle_causes(ctx)
    cause = result.get("cause")
    consistent = (cause in truth) if truth else None
    nums = [abs(float(x)) for x in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", str(result.get("evidence") or ""))]
    have = _numbers(ctx)
    grounded = sum(1 for n in nums if any(abs(n - h) <= max(0.011, 0.02 * h) for h in have))
    return {"consistent": consistent, "cited": len(nums), "grounded": grounded}
