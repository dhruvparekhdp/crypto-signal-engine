"""Local-LLM trade reviewer for the swing book (owner, 20 Oct 2026: "test all the strategy with live simulator with local
llm"). Research only. Does a model reading the chart before each trade (TAKE / SKIP) improve the live strategies?

    python -m scripts.llm_review prepare [--since 2025-01-01]    # Mac: data/lab/llm_review/prompts.jsonl
    python3 llm_review.py run PROMPTS OUT_DIR [--model qwen3:8b]  # ThinkPad: standard library only, resumable
    python -m scripts.llm_review apply                            # Mac: data/lab/live_sim_llm.json

The model never sees a coin name, a date or a price - only % moves, so it cannot recognise a famous day. Signals are
from 2025-26 only, after the strategies were chosen. Each prompt holds the last 30 bars (close-to-close %, range %,
volume vs its 20-bar average), trend (vs the 50 and 200-bar averages, 50-bar slope), RSI, ATR, the distance to the
20-bar high and low, BTC's last 7 and 30 days and its volatility rank, and the trade plan (stop, 3R target, 7 days).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

SYSTEM = ("You are a cautious, disciplined swing trader reviewing ONE trade a rules-based system wants to take on a "
          "crypto perpetual future. You see only anonymised numbers. Decide whether to TAKE or SKIP it. Skip when the "
          "chart argues against the trade (fighting a strong trend, an exhausted move, a stop inside the noise, a market "
          "in turmoil); take it otherwise. Answer only with JSON: "
          '{"decision": "TAKE" or "SKIP", "confidence": 0-100, "reason": "at most 20 words"}')
FAMILY = {"vol_breakout": "volume-surge breakout", "keltner_break": "Keltner channel breakout",
          "donchian": "100-bar Donchian channel breakout", "ichimoku": "Ichimoku cloud breakout"}


# ------------------------------------------------------------------------------------------------ Mac: prepare / apply
def prepare(since: str = "2025-01-01") -> None:
    import numpy as np
    import pandas as pd

    from analysis.lab import features as F
    from analysis.lab.data import load_bars
    t = pd.read_pickle("data/lab/live_sim_trades.pkl")
    t = t[(t.entry_t >= pd.Timestamp(since, tz="UTC").value // 10**6) & t.filter_ok].reset_index(drop=True)
    btc = load_bars("BTCUSDT", "1d")
    bars = {}
    out = Path("data/lab/llm_review")
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(out / "prompts.jsonl", "w") as fh:
        for row in t.itertuples():
            sym = "PAXGUSDT" if row.coin == "XAUT" else row.coin + "USDT"
            key = (sym, row.tf)
            if key not in bars:
                b = load_bars(sym, row.tf)
                bars[key] = (b, F.ema(b.c, 50), F.ema(b.c, 200), F.atr(b.h, b.l, b.c), F.rsi(b.c, 14))
            b, e50, e200, atr, rsi = bars[key]
            i = int(np.searchsorted(b.t, row.sig_t, side="left"))     # the signal bar (it closed at sig_t + tf)
            if i < 210 or i >= len(b.t):
                continue
            c = b.c[i - 30:i + 1]
            chg = np.round(np.diff(c) / c[:-1] * 100, 2).tolist()
            rng = np.round((b.h[i - 29:i + 1] - b.l[i - 29:i + 1]) / b.c[i - 29:i + 1] * 100, 2).tolist()
            vavg = np.convolve(b.v, np.ones(20) / 20, mode="full")[:len(b.v)]
            vol = np.round(b.v[i - 29:i + 1] / np.where(vavg[i - 30:i] > 0, vavg[i - 30:i], 1), 2).tolist()
            j = int(np.searchsorted(btc.t + 86_400_000, row.sig_t, side="right")) - 1
            btc7 = (btc.c[j] / btc.c[j - 7] - 1) * 100 if j >= 7 else None
            btc30 = (btc.c[j] / btc.c[j - 30] - 1) * 100 if j >= 30 else None
            facts = {
                "timeframe": row.tf, "setup": FAMILY.get(row.strategy, row.strategy),
                "side": "LONG" if row.side > 0 else "SHORT",
                "last_30_bars_close_change_pct_oldest_first": chg, "last_30_bars_range_pct": rng,
                "last_30_bars_volume_vs_20bar_avg": vol,
                "price_vs_50bar_avg_pct": round((b.c[i] / e50[i] - 1) * 100, 2),
                "price_vs_200bar_avg_pct": round((b.c[i] / e200[i] - 1) * 100, 2),
                "50bar_avg_slope_last_10_bars_pct": round((e50[i] / e50[i - 10] - 1) * 100, 2),
                "rsi_14": round(float(rsi[i]), 1), "atr_14_pct": round(float(atr[i] / b.c[i] * 100), 2),
                "distance_to_20bar_high_pct": round((b.h[i - 19:i + 1].max() / b.c[i] - 1) * 100, 2),
                "distance_to_20bar_low_pct": round((1 - b.l[i - 19:i + 1].min() / b.c[i]) * 100, 2),
                "btc_last_7_days_pct": round(btc7, 1) if btc7 is not None else None,
                "btc_last_30_days_pct": round(btc30, 1) if btc30 is not None else None,
                "btc_volatility_rank_0_to_1": round(float(row.btc_vol_rank), 2),
                "plan": {"stop_distance_pct": round(float(row.stop_frac) * 100, 2), "target": "3 x the stop distance",
                         "max_hold": "7 days", "stop_moves_to_breakeven_at": "+1R"},
            }
            fh.write(json.dumps({"key": f"{row.spec}|{row.coin}|{int(row.entry_t)}", "facts": facts}) + "\n")
            n += 1
    print(f"{n} prompts (signals from {since}, after the BTC filter) -> {out / 'prompts.jsonl'}")


def apply() -> None:
    import itertools

    import pandas as pd

    from scripts.live_sim import RISKS, WALLETS, simulate
    res = {}
    for f in sorted(Path("data/lab/llm_review").glob("results*.jsonl")):
        for ln in f.read_text().splitlines():
            try:
                r = json.loads(ln)
                res[r["key"]] = r
            except (ValueError, KeyError):
                continue
    t = pd.read_pickle("data/lab/live_sim_trades.pkl")
    t = t[t.entry_t >= pd.Timestamp("2025-01-01", tz="UTC").value // 10**6].copy()
    t["key"] = [f"{s}|{c}|{int(e)}" for s, c, e in zip(t.spec, t.coin, t.entry_t)]
    t["decision"] = t.key.map(lambda k: (res.get(k) or {}).get("decision"))
    t["llm_conf"] = t.key.map(lambda k: (res.get(k) or {}).get("confidence"))
    reviewed = t[t.decision.notna() & t.filter_ok]
    sig = {d: {"signals": int(len(g)), "win_pct": round(100 * float((g.r_net > 0).mean()), 1),
               "avg_r": round(float(g.r_net.mean()), 3)} for d, g in reviewed.groupby("decision")}
    gated = t[(t.decision != "SKIP")]                         # SKIP removed; not reviewed yet = kept
    combos = [c for k in range(1, 9) for c in itertools.combinations(range(8), k)]
    rows = []
    for c in combos:
        for r_ in RISKS:
            for w in WALLETS:
                a = simulate(t[t.spec.isin(c)], w, r_)
                b = simulate(gated[gated.spec.isin(c)], w, r_)
                rows.append({"combo": list(c), "risk": r_, "wallet": w, "without": a, "with_llm": b})
    Path("data/lab/live_sim_llm.json").write_text(json.dumps({
        "reviewed": int(len(reviewed)), "of": int(t.filter_ok.sum()), "by_decision": sig,
        "reasons": [{"key": k, **{x: v.get(x) for x in ("decision", "confidence", "reason")}} for k, v in list(res.items())[:400]],
        "runs": rows}, default=float))
    print("reviewed", len(reviewed), "by decision", sig)


# ----------------------------------------------------------------------------------- ThinkPad: run (standard library)
def _ask(model: str, facts: dict, host: str, think: bool = False) -> dict:
    body = {"model": model, "stream": False, "format": "json", "think": think,
            # num_gpu 0: the ThinkPad's Intel iGPU (Vulkan) runs out of memory for a 14B model's KV cache; CPU + RAM fits
            "options": {"temperature": 0, "num_ctx": 8192 if think else 4096, "num_gpu": 0},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": json.dumps(facts, separators=(",", ":"))}]}
    req = urllib.request.Request(f"{host}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        msg = json.loads(r.read())["message"]["content"]
    d = json.loads(msg[msg.find("{"): msg.rfind("}") + 1])
    dec = str(d.get("decision", "")).upper()
    return {"decision": dec if dec in ("TAKE", "SKIP") else "TAKE", "confidence": d.get("confidence"),
            "reason": str(d.get("reason", ""))[:200], "parsed": dec in ("TAKE", "SKIP")}


def run(prompts: str, out_dir: str, model: str = "qwen3:8b", host: str = "http://127.0.0.1:11434",
        think: bool = False) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    res_path = out / f"results_{model.replace(':', '_')}.jsonl"
    done = set()
    if res_path.exists():
        for ln in res_path.read_text().splitlines():
            try:
                done.add(json.loads(ln)["key"])
            except (ValueError, KeyError):
                pass
    items = [json.loads(ln) for ln in Path(prompts).read_text().splitlines() if ln.strip()]
    todo = [x for x in items if x["key"] not in done]
    started, times, counts = time.time(), [], {"TAKE": 0, "SKIP": 0, "errors": 0}
    with open(res_path, "a") as fh:
        for n, it in enumerate(todo, 1):
            t0 = time.time()
            try:
                r = _ask(model, it["facts"], host, think)
            except Exception as e:  # noqa: BLE001 - one bad answer must not stop the batch
                counts["errors"] += 1
                r = {"decision": "TAKE", "confidence": None, "reason": f"error: {str(e)[:120]}", "parsed": False}
            r.update(key=it["key"], model=model, seconds=round(time.time() - t0, 1))
            fh.write(json.dumps(r) + "\n")
            fh.flush()
            counts[r["decision"]] = counts.get(r["decision"], 0) + 1
            times.append(time.time() - t0)
            avg = sum(times[-50:]) / len(times[-50:])
            (out / "progress.json").write_text(json.dumps({
                "model": model + (" (thinking)" if think else ""), "done": len(done) + n, "total": len(items), "this_run": n, "avg_seconds": round(avg, 1),
                "eta_minutes": round(avg * (len(todo) - n) / 60, 1), "counts": counts, "last": r,
                "started": started, "updated": time.time()}))


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "prepare":
        prepare(sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--since" else "2025-01-01")
    elif len(sys.argv) >= 4 and sys.argv[1] == "run":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("stage")
        ap.add_argument("prompts")
        ap.add_argument("out_dir")
        ap.add_argument("--model", default="qwen3:8b")
        ap.add_argument("--think", action="store_true")
        a = ap.parse_args()
        run(a.prompts, a.out_dir, a.model, think=a.think)
    elif len(sys.argv) >= 2 and sys.argv[1] == "apply":
        apply()
    else:
        print(__doc__)
