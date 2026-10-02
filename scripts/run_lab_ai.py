"""Review a sample of finished backtest trades with LOCAL Ollama models and time it.

    python -m scripts.run_lab_ai --generate --hours 8 --models qwen3:8b,llama3.2:3b --per-model 12
    python -m scripts.run_lab_ai --trades data/lab/runs/full_5y/trades.parquet --from 2026-09-30T16:00 --per-model 20

Cloud models (names ending in :cloud) are refused: they leave the machine and may cost money.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.lab_ai import context, review
from analysis.lab_ai.web import headlines

THINKING = {"deepseek-r1": True, "qwen3": None}   # None: pass think only when --think is given


def to_ms(s):
    return int(pd.Timestamp(s, tz="UTC").timestamp() * 1000)


def generate(start, end, root):
    from analysis.lab.runner import RunSpec, run_lab
    from analysis.lab.simulate import ExitModel
    from analysis.lab.strategies import REGISTRY
    warm = (pd.Timestamp(start) - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
    spec = RunSpec(strategies=list(REGISTRY), exits=[ExitModel(), ExitModel(stop_atr=3.0, rr=3.0, max_hold_min=10080)],
                   start=warm, end=None, root=root)
    res = run_lab(spec, workers=10, out_dir=None)
    t = res["trades"]
    return t[(t.entry_t >= to_ms(start)) & (t.entry_t < to_ms(end))].reset_index(drop=True), res["elapsed_s"]


def stratified(df, n, seed=3):
    rng = np.random.default_rng(seed)
    groups = {k: g.sample(frac=1, random_state=int(rng.integers(1e9))) for k, g in df.groupby("strategy")}
    out, i = [], 0
    while len(out) < min(n, len(df)):
        for k in list(groups):
            if i < len(groups[k]) and len(out) < n:
                out.append(groups[k].iloc[i])
        i += 1
        if i > 10000:
            break
    return pd.DataFrame(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--trades", default=None)
    ap.add_argument("--from", dest="t_from", default=None)
    ap.add_argument("--to", dest="t_to", default=None)
    ap.add_argument("--models", default="qwen3:8b,llama3.2:3b")
    ap.add_argument("--per-model", type=int, default=12)
    ap.add_argument("--think", action="store_true", help="enable thinking for models that support turning it on")
    ap.add_argument("--num-predict", type=int, default=300)
    ap.add_argument("--web", action="store_true")
    ap.add_argument("--root", default="data/lake")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--out", default="data/lab/ai")
    a = ap.parse_args()
    models = [m for m in a.models.split(",") if m]
    bad = [m for m in models if m.endswith(":cloud")]
    if bad:
        raise SystemExit(f"refusing cloud models {bad}: use local models only")

    from analysis.lab.data import load_bars
    last = int(load_bars("BTCUSDT", "1m", a.root).t[-1]) + 60_000
    end = a.t_to or str(pd.to_datetime(last, unit="ms"))
    start = a.t_from or str(pd.to_datetime(last - int(a.hours * 3_600_000), unit="ms"))
    gen_s = None
    if a.generate:
        trades, gen_s = generate(start, end, a.root)
        print(f"backtest of all strategies up to {end[:16]} took {gen_s:.0f}s; "
              f"{len(trades)} trades entered in the last {a.hours:g}h ({start[:16]} -> {end[:16]})")
    else:
        trades = pd.read_parquet(a.trades)
        trades = trades[(trades.entry_t >= to_ms(start)) & (trades.entry_t < to_ms(end))].reset_index(drop=True)
    if trades.empty:
        raise SystemExit("no trades in that window")
    sample = stratified(trades, a.per_model)
    print(f"reviewing {len(sample)} trades per model across {sample.strategy.nunique()} strategies\n")
    ctxs = []
    t0 = time.time()
    for _, row in sample.iterrows():
        c = context.build(row, a.root)
        news, note = ([], "off")
        if a.web:
            news, note = headlines(row["symbol"], pd.to_datetime(int(row["entry_t"]), unit="ms").to_pydatetime())
        ctxs.append((row, c, news, note))
    print(f"context built in {time.time() - t0:.1f}s" + (f" (web: {Counter(n[3] for n in ctxs)})" if a.web else ""))

    Path(a.out).mkdir(parents=True, exist_ok=True)
    rows = []
    for m in models:
        think = None
        base = m.split(":")[0]
        if base in THINKING:
            think = THINKING[base] if THINKING[base] is not None else (True if a.think else False)
        print(f"\n== {m} (think={think})")
        for i, (row, c, news, note) in enumerate(ctxs, 1):
            r = review.review(c, m, think=think, num_predict=a.num_predict, news=news, host=a.host)
            r.update(strategy=row["strategy"], symbol=row["symbol"], r_net=float(row["r_net"]), **review.grade(c, r))
            rows.append(r)
            print(f"  {i:2}/{len(ctxs)} {row['strategy']:16} {r['wall_s']:6.1f}s {r.get('out_tokens', 0):4} tok "
                  f"{r.get('tok_per_s', 0):5.1f} t/s {'ok ' if r['ok'] else 'BAD'} {r.get('cause')}")
    df = pd.DataFrame(rows)
    df.to_json(Path(a.out) / "reviews.jsonl", orient="records", lines=True)
    print("\n== SUMMARY")
    g = df.groupby("model").agg(n=("ok", "size"), valid=("ok", "mean"), sec=("wall_s", "mean"), tok=("out_tokens", "mean"),
                                tps=("tok_per_s", "mean"))
    g["checkable"] = df.groupby("model").consistent.apply(lambda s: int(s.notna().sum()))
    g["label_ok"] = df.groupby("model").consistent.apply(lambda s: float(s.dropna().astype(bool).mean()) if s.notna().any() else float("nan"))
    g["grounded"] = df.groupby("model").apply(lambda d: d.grounded.sum() / max(1, d.cited.sum()), include_groups=False)
    n_all = len(trades)
    g["trades_per_hour"] = 3600 / g.sec
    g["hrs_for_window"] = n_all * g.sec / 3600
    pd.set_option("display.width", 200, "display.float_format", "{:.2f}".format)
    print(g.to_string())
    print(f"\nwindow has {n_all} trades; 5-year run has about 5,400,000 trades.")
    for m, row in g.iterrows():
        print(f"  {m}: all {n_all} window trades = {row.hrs_for_window:.1f}h; 5,400,000 trades = {5.4e6 * row.sec / 3600 / 24 / 365:.0f} years")
    for m in models:
        d = df[df.model == m]
        print(f"\ncauses by {m}:", dict(Counter(d.cause.dropna())))
    json.dump({"gen_seconds": gen_s, "window_trades": n_all}, open(Path(a.out) / "meta.json", "w"))


if __name__ == "__main__":
    main()
