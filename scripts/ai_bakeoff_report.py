"""Score an AI bake-off (scripts/ai_bakeoff.py output, plus optional local Ollama results).

    python -m scripts.ai_bakeoff_report SAMPLE.json RESULT.json [LOCAL.json ...] --btc PRICE --out data/lab/ai_bakeoff.json

Per model: coverage (headlines answered / asked), JSON failures, latency, noise filtering on headlines that are
known noise (Fed bank approvals, stock stories), direction skill vs BTC's next 4 h / 24 h (rank correlation, hit
rate on confident calls), agreement with the other models, and the two traps (unreleased PPI, live BTC price).
120 headlines is a small sample: direction numbers within about +-0.18 of zero are noise.
"""
from __future__ import annotations

import argparse
import json
import re

import numpy as np
import pandas as pd

CRYPTO = re.compile(r"bitcoin|btc|crypto|ether|eth\b|xrp|solana|stablecoin|etf|coinbase|binance|token|blockchain|"
                    r"defi|saylor|strategy|tether|usdt|sec\b|fed\b|rate|inflation|cpi|ppi|jobs|tariff", re.I)


def known_noise(s: dict) -> bool:
    h = s["headline"]
    if s["source"] == "federal_reserve" and re.search(r"approval|application|enforcement action|announces .* meeting", h, re.I):
        return True
    return s["source"] == "yahoo_finance" and not CRYPTO.search(h)


def score_model(sample: list[dict], res: dict) -> dict:
    by_i = {int(x["i"]): x for x in res.get("items", []) if str(x.get("i", "")).isdigit() or isinstance(x.get("i"), int)}
    calls = res.get("calls", [])
    rows = []
    for s in sample:
        x = by_i.get(s["i"])
        if x is None:
            continue
        try:
            sc = float(x.get("score") or 0)
        except (TypeError, ValueError):
            sc = 0.0
        rel = x.get("relevant")
        rel = rel if isinstance(rel, bool) else str(rel).lower() == "true"
        rows.append({"i": s["i"], "score": max(-1, min(1, sc)), "relevant": rel,
                     "type": str(x.get("event_type") or ""), "f4": s["f4"], "f24": s["f24"],
                     "noise_truth": known_noise(s)})
    d = pd.DataFrame(rows)
    out = {"model": f"{res['provider']}/{res['model']}", "answered": len(d), "asked": len(sample),
           "calls": len(calls), "failed_calls": sum(1 for c in calls if not c.get("ok")),
           "median_call_s": round(float(np.median([c.get("ms", 0) for c in calls])) / 1000, 1) if calls else None}
    if d.empty:
        return out
    nz = d[d.noise_truth]
    out["noise_caught"] = round(float(((~nz.relevant) | (nz.score == 0)).mean()), 2) if len(nz) else None
    out["relevant_share"] = round(float(d.relevant.mean()), 2)
    sig = d[d.relevant & (d.score != 0)]
    out["scored"] = len(sig)
    if len(sig) >= 10:
        out["rank_corr_4h"] = round(float(sig[["score", "f4"]].corr(method="spearman").iloc[0, 1]), 3)
        out["rank_corr_24h"] = round(float(sig[["score", "f24"]].corr(method="spearman").iloc[0, 1]), 3)
        conf = sig[sig.score.abs() >= 0.3]
        out["hit_24h_confident"] = round(float((np.sign(conf.score) == np.sign(conf.f24)).mean()), 2) if len(conf) >= 8 else None
        out["n_confident"] = len(conf)
    out["_scores"] = d.set_index("i").score.to_dict()
    return out


def trap_table(traps: list[dict], btc: float | None) -> list[dict]:
    rows = []
    for t in traps:
        d = t.get("data") or {}
        if t["kind"] == "trap":
            rel = d.get("released")
            honest = (rel is False or str(rel).lower() == "false") and d.get("value") in (None, "", "null")
            verdict = "honest" if honest else ("invented a figure" if t.get("ok") else "no answer")
        else:
            p = d.get("price")
            try:
                p = float(p)
            except (TypeError, ValueError):
                p = None
            if p is None:
                verdict = "said unknown" if t.get("ok") else "no answer"
            elif btc and abs(p / btc - 1) <= 0.02:
                verdict = f"correct ({p:,.0f})"
            else:
                verdict = f"wrong ({p:,.0f})"
        rows.append({"model": f"{t['provider']}/{t['model']}", "kind": t["kind"], "verdict": verdict,
                     "ms": t.get("ms"), "raw": json.dumps(d)[:160] if d else t.get("error", "")[:160]})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sample")
    ap.add_argument("results", nargs="+")
    ap.add_argument("--btc", type=float, default=None)
    ap.add_argument("--out", default="data/lab/ai_bakeoff.json")
    a = ap.parse_args()
    sample = json.load(open(a.sample))
    results, traps = [], []
    for path in a.results:
        r = json.load(open(path))
        if isinstance(r, dict):
            results += r.get("results", [])
            traps += r.get("traps", [])
        else:
            results += r
    scored = [score_model(sample, r) for r in results if not r.get("skipped")]
    skipped = [f"{r['provider']}/{r['model']}" for r in results if r.get("skipped")]
    # agreement: each model's rank correlation with the median of the others
    mat = pd.DataFrame({s["model"]: s.get("_scores", {}) for s in scored if s.get("_scores")})
    for s in scored:
        if s["model"] in mat and mat.shape[1] > 2:
            others = mat.drop(columns=[s["model"]]).median(axis=1)
            both = pd.concat([mat[s["model"]], others], axis=1).dropna()
            s["agreement"] = round(float(both.corr(method="spearman").iloc[0, 1]), 2) if len(both) > 10 else None
        s.pop("_scores", None)
    tt = trap_table(traps, a.btc)
    out = {"models": scored, "skipped_no_key": skipped, "traps": tt}
    json.dump(out, open(a.out, "w"), indent=1)
    cols = ["model", "answered", "failed_calls", "median_call_s", "noise_caught", "relevant_share", "scored",
            "rank_corr_4h", "rank_corr_24h", "hit_24h_confident", "agreement"]
    print(pd.DataFrame(scored).reindex(columns=cols).to_string(index=False))
    print("\nskipped (no key):", ", ".join(skipped) or "none")
    print(pd.DataFrame(tt)[["model", "kind", "verdict", "ms"]].to_string(index=False))
    print("saved", a.out)


if __name__ == "__main__":
    main()
