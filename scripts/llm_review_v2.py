"""Local-LLM trade review, version 2 (owner, 21 Oct 2026). Research only.

Version 1 (scripts/llm_review.py) skipped 9 of the first 35 signals and all 9 won: its prompt said "skip an exhausted
move" without saying the system is trend-following, so high RSI and price far above the average - normal for a
breakout - read as danger; 23 of 35 answers were the same "confidence 75" template. Version 2:

  pre   blind, before the trade: what the system is and its 2021-24 base rates (48% of trades end in profit, winners
        +1.58R, losers -0.82R), summarised facts instead of 90 raw numbers, and a probability of profit - the cut-off
        is chosen from the data afterwards, not taken from the model's yes/no
  post  after the trade, with the outcome: why it won or lost, as a category + two sentences, whether it was
        predictable at entry, and one lesson (owner: "if trade won then why and lost then why")
The two calls never share a context, so the pre-trade answer cannot see the result. No coin names, dates or prices.

    python -m scripts.llm_review_v2 prepare                          # Mac: data/lab/llm_review/prompts_v2.jsonl
    python3 llm_review_v2.py run PROMPTS OUT_DIR [--model qwen3:14b]  # ThinkPad: standard library only, resumable
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

ORDER = ["ADA", "XRP", "SUI", "TIA", "XAUT",          # a Rs500 wallet can trade these
         "DOGE", "AVAX",                              # and these at Rs1,000
         "ETH", "BTC", "SOL"]                         # the big coins, for when the wallet grows
BASE = {"profit_pct": 48, "avg_win_r": 1.58, "avg_loss_r": -0.82, "target_pct": 16}   # 2021-24, live specs + filter
FAMILY = {"vol_breakout": "volume-surge breakout", "keltner_break": "Keltner channel breakout",
          "donchian": "100-bar Donchian channel breakout", "ichimoku": "Ichimoku cloud breakout"}

PRE_SYSTEM = (
    "You are a quantitative trading analyst reviewing ONE signal from a rules-based TREND-FOLLOWING BREAKOUT system "
    "on crypto perpetual futures (4h and 8h bars). Facts about this system from 2021-2024: about "
    f"{BASE['profit_pct']}% of its trades end in profit; winners average +{BASE['avg_win_r']}R and losers "
    f"{BASE['avg_loss_r']}R (1R = the distance to the stop); {BASE['target_pct']}% reach the full 3R target. It makes "
    "money by riding the breakouts that turn into trends. A breakout naturally comes with a strong recent move, a high "
    "RSI for longs or a low RSI for shorts, and price far from its averages - on their own these are normal, not "
    "warning signs. Estimate the probability (0-100) that THIS trade ends in profit. Start from the base rate and "
    "move away from it only for specific evidence in the numbers, such as: the trade going against the larger trend, "
    "BTC moving hard against the trade, a breakout on falling volume, a stop unusually small or large for the "
    "volatility, a market in a violent swing. Decision rule: TAKE unless your probability is more than 10 points "
    "below the base rate. You see anonymised numbers only - no coin name, date or price level. Answer in JSON: up to "
    "3 factors of at most 12 words each, and a reason of at most 30 words.")
PRE_SCHEMA = {"type": "object", "properties": {
    "p_profit": {"type": "integer", "minimum": 0, "maximum": 100},
    "decision": {"type": "string", "enum": ["TAKE", "SKIP"]},
    "factors": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
    "reason": {"type": "string"}}, "required": ["p_profit", "decision", "factors", "reason"]}

CATEGORIES = ["trend continued", "reversal", "no follow-through", "market-wide move", "volatility spike",
              "gave back gains"]


def rule_category(res: dict) -> str:
    """Why a trade won or lost, decided from its numbers (21 Oct 2026: left to the model, it called a short that lost
    while BTC rose 6.6% a 'reversal', and never used 'gave back gains' for trades that reached +1.7R to +3R and the
    ladder closed for +0.04R to +1.09R). The model then explains within this category."""
    r, best = res["r_after_costs"], res["best_point_r"]
    btc = res.get("btc_move_with_trade_pct") or 0.0          # + = BTC moved the trade's way
    after, ex = res.get("price_1_day_after_exit_r"), res.get("exit_kind")
    if ex == "trail" and best - r >= 1.0:
        return "gave back gains"
    if r > 0:
        if ex == "time" and r < 0.5:
            return "no follow-through"
        return "market-wide move" if btc >= 3.0 else "trend continued"
    if btc <= -3.0:
        return "market-wide move"
    if ex == "stop" and after is not None and after >= 0:
        return "volatility spike"                              # stopped out, and a day later price was back past entry
    if ex == "time" and r > -0.5:
        return "no follow-through"
    return "reversal"


POST_SYSTEM = (
    "You are reviewing a FINISHED trade from a trend-following breakout system on crypto perpetual futures, to explain "
    "WHY it won or lost. You get the facts at entry and what happened after. The main cause ('category') has already "
    "been decided from the numbers by fixed rules: 'trend continued' (moved the trade's way and kept going), "
    "'reversal' (turned against the trade), 'no follow-through' (went nowhere; small result at the time limit), "
    "'market-wide move' (BTC moved 3% or more with or against the trade and carried the coin), 'volatility spike' "
    "(the stop was hit, yet a day later price was back past the entry), 'gave back gains' (reached a good profit, "
    "then the profit ladder closed it at least 1R lower). Explain the result in at most two sentences that cite the "
    "numbers and are consistent with that category; name one other factor that contributed, if any. Judge only from "
    "the entry facts whether the result was predictable (yes, partly, no) - a win is not predictable just because it "
    "won. Give one lesson of at most 15 words. Numbers are anonymised. Answer in JSON.")
POST_SCHEMA = {"type": "object", "properties": {
    "why": {"type": "string"},
    "other_factor": {"type": "string"},
    "predictable": {"type": "string", "enum": ["yes", "partly", "no"]},
    "lesson": {"type": "string"}}, "required": ["why", "other_factor", "predictable", "lesson"]}
POST_VERSION = 2

# Qwen3's own advice for non-thinking mode, plus a penalty against the templated answers v1 produced
OPTIONS = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.0, "seed": 42,
           "num_ctx": 4096, "num_gpu": 20, "num_thread": 10}


# ------------------------------------------------------------------------------------------------ Mac: prepare
def prepare(since: str = "2025-01-01") -> None:
    import numpy as np
    import pandas as pd

    from analysis.lab import features as F
    from analysis.lab.data import INTERVAL_MS, load_bars
    H = 3_600_000
    t = pd.read_pickle("data/lab/live_sim_trades.pkl")
    t = t[(t.entry_t >= pd.Timestamp(since, tz="UTC").value // 10**6) & t.filter_ok & t.coin.isin(ORDER)].copy()
    t["order"] = t.coin.map({c: i for i, c in enumerate(ORDER)})
    t = t.sort_values(["order", "entry_t"]).reset_index(drop=True)
    btc_d, btc_h = load_bars("BTCUSDT", "1d"), load_bars("BTCUSDT", "1h")
    btc_sma200 = F.sma(btc_d.c, 200)
    cache: dict = {}

    def pct(a, b):
        return round((a / b - 1) * 100, 2)

    out = Path("data/lab/llm_review")
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(out / "prompts_v2.jsonl", "w") as fh:
        for row in t.itertuples():
            sym = "PAXGUSDT" if row.coin == "XAUT" else row.coin + "USDT"
            if (sym, row.tf) not in cache:
                b = load_bars(sym, row.tf)
                cache[(sym, row.tf)] = (b, F.sma(b.c, 20), F.ema(b.c, 50), F.ema(b.c, 200),
                                        F.atr(b.h, b.l, b.c), F.rsi(b.c, 14))
            if (sym, "1h") not in cache:
                cache[(sym, "1h")] = load_bars(sym, "1h")
            b, s20, e50, e200, atr, rsi = cache[(sym, row.tf)]
            # the lab's sig_t is the signal bar's CLOSE time; the bar that OPENS then is the entry bar - its data is
            # the future (21 Oct 2026: the first draft read it, so a long breakout showed a -7.9% "last bar")
            i = int(np.searchsorted(b.t, row.sig_t - INTERVAL_MS[row.tf], side="left"))
            if i >= len(b.t) or int(b.t[i]) + INTERVAL_MS[row.tf] != int(row.sig_t):
                continue
            if i < 210 or i >= len(b.t):
                continue
            c = b.c
            v20 = b.v[i - 20:i].mean()
            atr50 = np.nanmean(atr[i - 50:i])
            hi20, lo20 = b.h[i - 19:i + 1].max(), b.l[i - 19:i + 1].min()
            hi100, lo100 = b.h[i - 99:i + 1].max(), b.l[i - 99:i + 1].min()
            moves = np.diff(c[i - 30:i + 1]) / c[i - 30:i] * 100
            jd = int(np.searchsorted(btc_d.t + 86_400_000, row.sig_t, side="right")) - 1
            pre = {
                "setup": FAMILY.get(row.strategy, row.strategy), "timeframe": row.tf,
                "side": "LONG" if row.side > 0 else "SHORT",
                "move_pct": {"last_bar": pct(c[i], c[i - 1]), "3_bars": pct(c[i], c[i - 3]), "6_bars": pct(c[i], c[i - 6]),
                             "12_bars": pct(c[i], c[i - 12]), "30_bars": pct(c[i], c[i - 30])},
                "up_bars_share": {"last_12": round(float((moves[-12:] > 0).mean()), 2),
                                  "last_30": round(float((moves > 0).mean()), 2)},
                "largest_single_bar_move_last_30_pct": round(float(np.abs(moves).max()), 2),
                "price_vs_avg_pct": {"20_bar": pct(c[i], s20[i]), "50_bar": pct(c[i], e50[i]), "200_bar": pct(c[i], e200[i])},
                "avg50_slope_last_10_bars_pct": pct(e50[i], e50[i - 10]),
                "rsi_14": round(float(rsi[i]), 1),
                "atr_14_pct": round(float(atr[i] / c[i] * 100), 2),
                "atr_vs_its_50_bar_average": round(float(atr[i] / atr50), 2) if atr50 > 0 else None,
                "volume_vs_20_bar_avg": {"last_bar": round(float(b.v[i] / v20), 2) if v20 > 0 else None,
                                         "last_5_bars": round(float(b.v[i - 4:i + 1].mean() / v20), 2) if v20 > 0 else None},
                "position_in_range_0_low_1_high": {"20_bar": round(float((c[i] - lo20) / (hi20 - lo20)), 2) if hi20 > lo20 else None,
                                                   "100_bar": round(float((c[i] - lo100) / (hi100 - lo100)), 2) if hi100 > lo100 else None},
                "btc": {"last_day_pct": pct(btc_d.c[jd], btc_d.c[jd - 1]), "last_7_days_pct": pct(btc_d.c[jd], btc_d.c[jd - 7]),
                        "last_30_days_pct": pct(btc_d.c[jd], btc_d.c[jd - 30]),
                        "above_200_day_average": bool(btc_d.c[jd] > btc_sma200[jd]),
                        "volatility_rank_0_to_1": round(float(row.btc_vol_rank), 2)},
                "plan": {"stop_distance_pct": round(float(row.stop_frac) * 100, 2),
                         "stop_in_atrs": round(float(row.stop_frac * c[i] / atr[i]), 1) if atr[i] > 0 else None,
                         "target": "3 x the stop distance", "max_hold": "7 days",
                         "profit_ladder": "stop to breakeven at +1R, then locks +0.5R / +1R / +1.5R at 1.5R / 2R / 2.5R"},
            }
            # ---- what happened (post-mortem only): the coin's path in R at 25/50/75/100% of the hold, BTC during the
            # trade, best and worst points, and where price was a day after the exit
            h = cache[(sym, "1h")]
            side, entry, risk = row.side, row.entry, row.stop_frac * row.entry
            hours = max(1, int(round((row.exit_t - row.entry_t) / H)))

            def r_at(ms):
                j = int(np.searchsorted(h.t + H, ms, side="right")) - 1
                return round(float(side * (h.c[j] - entry) / risk), 2) if 0 <= j < len(h.c) else None

            def btc_at(ms):
                j = int(np.searchsorted(btc_h.t + H, ms, side="right")) - 1
                return btc_h.c[j] if 0 <= j < len(btc_h.c) else None

            b0, b1 = btc_at(row.entry_t), btc_at(row.exit_t)
            btc_move = pct(b1, b0) if b0 and b1 else None
            best_r = round(float(row.mfe / row.stop_frac), 2)
            result = {
                "outcome": "WON" if row.r_net > 0 else "LOST", "r_after_costs": round(float(row.r_net), 2),
                "exit": {"target": "hit the 3R target", "stop": "hit the stop", "trail": "closed by the profit ladder",
                         "time": "closed at the 7-day limit"}.get(row.reason, row.reason), "exit_kind": row.reason,
                "hours_held": hours,
                "best_point_r": best_r, "worst_point_r": round(float(-row.mae / row.stop_frac), 2),
                "gave_back_from_best_r": round(best_r - float(row.r_net), 2),
                "path_r_at_25_50_75_100_pct_of_hold": [r_at(row.entry_t + int(f * (row.exit_t - row.entry_t))) for f in (0.25, 0.5, 0.75, 1.0)],
                "btc_move_during_trade_pct": btc_move,
                "btc_move_with_trade_pct": round(btc_move * side, 2) if btc_move is not None else None,
                "price_1_day_after_exit_r": r_at(row.exit_t + 24 * H)}
            result["category"] = rule_category(result)
            post = {**pre, "result": result}
            fh.write(json.dumps({"key": f"{row.spec}|{row.coin}|{int(row.entry_t)}", "coin": row.coin,
                                 "pre": pre, "post": post}) + "\n")
            n += 1
    print(f"{n} prompts -> {out / 'prompts_v2.jsonl'}; by coin {t.coin.value_counts().reindex(ORDER).to_dict()}")


# ----------------------------------------------------------------------------------- ThinkPad: run (standard library)
THINK_BUDGET = 400          # "medium" (owner, 21 Oct 2026): qwen3 has thinking on/off only, so the depth is a budget
THINK_OPTIONS = {**OPTIONS, "temperature": 0.6, "top_p": 0.95}     # Qwen3's advice for thinking mode
CUT = "\n\nConsidering the limited time, I have to give the answer now.\n</think>\n\n"


def _generate(host: str, body: dict) -> dict:
    req = urllib.request.Request(f"{host}/api/generate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.loads(r.read())


def _chat_thinking(model: str, system: str, facts: dict, schema: dict, host: str) -> tuple[dict, float, str]:
    """Think for up to THINK_BUDGET tokens, then answer in JSON (Qwen3's thinking-budget method, on the raw ChatML
    prompt): if the thinking has not ended by the budget, it is closed with Qwen's own 'give the answer now' line."""
    t0 = time.time()
    head = (f"<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n"
            f"{json.dumps(facts, separators=(',', ':'))}<|im_end|>\n<|im_start|>assistant\n<think>\n")
    r1 = _generate(host, {"model": model, "raw": True, "stream": False, "prompt": head,
                          "options": {**THINK_OPTIONS, "num_predict": THINK_BUDGET}})
    # Ollama returns the thinking in its own field (21 Oct 2026: reading only "response" threw it away, so the
    # answer was written without it); "response" holds whatever came after </think>, empty when the budget cut it
    thought, rest = r1.get("thinking") or "", r1.get("response") or ""
    if "</think>" in rest:                                  # older Ollama: everything in "response"
        extra, rest = rest.split("</think>", 1)
        thought += extra
    if rest.strip() and "{" in rest and "}" in rest:       # it finished thinking inside the budget and answered
        try:
            return json.loads(rest[rest.find("{"): rest.rfind("}") + 1]), time.time() - t0, thought.strip()
        except ValueError:
            pass
    ended = r1.get("done_reason") != "length"
    prompt = head + thought + ("\n</think>\n\n" if ended else CUT)
    ans = _generate(host, {"model": model, "raw": True, "stream": False, "prompt": prompt, "format": schema,
                           "options": {**OPTIONS, "num_predict": 400}})["response"]
    return json.loads(ans[ans.find("{"): ans.rfind("}") + 1]), time.time() - t0, thought.strip()


def _chat(model: str, system: str, facts: dict, schema: dict, host: str) -> tuple[dict, float]:
    body = {"model": model, "stream": False, "think": False, "format": schema, "options": OPTIONS,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": json.dumps(facts, separators=(",", ":"))}]}
    t0 = time.time()
    req = urllib.request.Request(f"{host}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        msg = json.loads(r.read())["message"]["content"]
    return json.loads(msg[msg.find("{"): msg.rfind("}") + 1]), time.time() - t0


def run(prompts: str, out_dir: str, model: str = "qwen3:14b", host: str = "http://127.0.0.1:11434") -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res_path = out / f"results_v2_{model.replace(':', '_')}.jsonl"
    done, old_pre = set(), {}
    if res_path.exists():
        for ln in res_path.read_text().splitlines():
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if rec.get("post_version") == POST_VERSION and rec.get("pre"):
                done.add(rec["key"])
            elif rec.get("pre"):
                old_pre[rec["key"]] = rec          # pre-trade answer kept; only the post-mortem is redone
    items = [json.loads(ln) for ln in Path(prompts).read_text().splitlines() if ln.strip()]
    todo = [x for x in items if x["key"] not in done]
    started, times, counts = time.time(), [], {"TAKE": 0, "SKIP": 0, "errors": 0}
    with open(res_path, "a") as fh:
        for n, it in enumerate(todo, 1):
            t0 = time.time()
            rec = {"key": it["key"], "coin": it.get("coin"), "model": model, "version": 2, "post_version": POST_VERSION}
            prev = old_pre.get(it["key"])
            if prev is not None:                    # answered before the post-mortem changed: keep its pre-trade review
                rec.update({k: prev[k] for k in ("pre", "pre_seconds", "thinking") if k in prev})
            else:
                try:
                    pre, s1, thought = _chat_thinking(model, PRE_SYSTEM, it["pre"], PRE_SCHEMA, host)
                    rec.update(pre=pre, pre_seconds=round(s1, 1), thinking=thought[-1500:])
                except Exception as e:  # noqa: BLE001 - one bad answer must not stop the batch
                    counts["errors"] += 1
                    rec["pre_error"] = str(e)[:160]
            if rec.get("pre"):
                d = rec["pre"].get("decision", "TAKE")
                counts[d] = counts.get(d, 0) + 1
            try:
                res = it["post"]["result"]
                post, s2 = _chat(model, POST_SYSTEM, it["post"], POST_SCHEMA, host)
                post["category"] = res["category"]
                rec.update(post=post, post_seconds=round(s2, 1), outcome=res["outcome"], r=res["r_after_costs"],
                           best_r=res["best_point_r"], btc_with_trade=res["btc_move_with_trade_pct"])
            except Exception as e:  # noqa: BLE001
                counts["errors"] += 1
                rec["post_error"] = str(e)[:160]
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            times.append(time.time() - t0)
            avg = sum(times[-30:]) / len(times[-30:])
            (out / "progress.json").write_text(json.dumps({
                "model": f"{model} (v2: pre-trade with medium thinking + post-mortem)", "done": len(done) + n, "total": len(items),
                "this_run": n, "avg_seconds": round(avg, 1), "eta_minutes": round(avg * (len(todo) - n) / 60, 1),
                "counts": counts, "last": rec, "started": started, "updated": time.time()}))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "prepare":
        prepare()
    elif len(sys.argv) >= 4 and sys.argv[1] == "run":
        run(sys.argv[2], sys.argv[3], sys.argv[5] if len(sys.argv) > 5 and sys.argv[4] == "--model" else "qwen3:14b")
    else:
        print(__doc__)
