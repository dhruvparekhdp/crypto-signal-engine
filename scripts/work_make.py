"""Build a work set for the gate experiment: contexts are computed once here (needs the lake), so the
laptops that do the reviewing need nothing but Ollama and Python.

    python -m scripts.work_make --name gate_vb --trades data/lab/runs/cand_4h_tp/trades.parquet \
        --cfg-contains "vol_breakout|z=3.0" --cfg-contains sl3.0_rr3.0 --n 150 --models qwen3:8b,deepseek-r1:8b
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.lab_ai import gate


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--trades", required=True)
    ap.add_argument("--cfg-contains", action="append", default=[], help="filter every spec must also match")
    ap.add_argument("--spec", action="append", default=[], help="strategy filter; --n trades are sampled from EACH spec")
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--models", default="qwen3:8b,deepseek-r1:8b")
    ap.add_argument("--think-tokens", type=int, default=700)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--root", default="data/lake")
    a = ap.parse_args()
    models = [m for m in a.models.split(",") if m]
    if any(m.endswith(":cloud") for m in models):
        raise SystemExit("local models only")
    full = pd.read_parquet(a.trades)
    for sub in a.cfg_contains:
        full = full[full.cfg.str.contains(sub, regex=False)]
    parts, pops = [], []
    rng = np.random.default_rng(a.seed)
    for spec in (a.spec or [""]):
        d_ = full[full.cfg.str.contains(spec, regex=False)] if spec else full
        if d_.empty:
            raise SystemExit(f"no trades match {spec!r}")
        pops.append(d_)
        parts.append(d_.iloc[np.sort(rng.choice(len(d_), size=min(a.n, len(d_)), replace=False))])
    df = pd.concat(pops)
    # shuffled, so whatever finishes first is a random cross-section of years and strategies, not just the oldest trades
    sample = pd.concat(parts).sample(frac=1, random_state=a.seed).reset_index(drop=True)
    lab = Path(os.environ.get("LAB_ROOT", "."))
    d = lab / "work" / a.name
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "items.jsonl", "w") as f:
        for i, row in sample.iterrows():
            ctx = gate.pretrade_context(row, a.root)
            f.write(json.dumps({"i": int(i), "key": f"{row['cfg']}|{row['symbol']}|{int(row['entry_t'])}",
                                "symbol": row["symbol"], "strategy": row["strategy"], "side": int(row["side"]),
                                "r_net": float(row["r_net"]), "r_gross": float(row["r_gross"]),
                                "entry_t": int(row["entry_t"]), "user": gate.prompt(ctx)}) + "\n")
    meta = {"name": a.name, "kind": "gate", "title": f"AI gate: {a.cfg_contains[0] if a.cfg_contains else 'all'}",
            "models": models, "n_items": int(len(sample)), "system": gate.SYSTEM, "schema": gate.SCHEMA,
            "think_tokens": a.think_tokens, "created": time.time(), "population_n": int(len(df)),
            "population_exp_r": float(df.r_net.mean()), "population_win": float((df.r_net > 0).mean())}
    (d / "meta.json").write_text(json.dumps(meta))
    # features for EVERY matching trade, so a cheap non-AI filter can be tested on the same data
    keys = set(f"{r['cfg']}|{r['symbol']}|{int(r['entry_t'])}" for _, r in sample.iterrows())
    with open(d / "population.jsonl", "w") as f:
        for _, row in df.iterrows():
            key = f"{row['cfg']}|{row['symbol']}|{int(row['entry_t'])}"
            ctx = gate.pretrade_context(row, a.root)
            f.write(json.dumps({"key": key, "in_sample": key in keys, "strategy": row["strategy"], "side": int(row["side"]),
                                "entry_t": int(row["entry_t"]), "r_net": float(row["r_net"]),
                                "features": gate.features(ctx, int(row["side"]))}) + "\n")
    print(f"work set {a.name}: {len(sample)} trades x {len(models)} models = {len(sample) * len(models)} tasks -> {d}")


if __name__ == "__main__":
    main()
