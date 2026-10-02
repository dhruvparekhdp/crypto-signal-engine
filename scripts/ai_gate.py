"""With-AI vs without-AI experiment on one strategy's finished trades, using local Ollama models only.

    python -m scripts.ai_gate --name gate_qwen --trades data/lab/runs/cand_4h_tp/trades.parquet \
        --cfg-contains "vol_breakout|z=3.0" --cfg-contains sl3.0_rr3.0 --n 150 --models qwen3:8b

A random sample (seeded, not picked by outcome) is shown to the model with outcomes hidden. Progress and the
running comparison go to status/ai_<name>.json for the dashboard. --shard i/N splits the sample across laptops.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.lab_ai import gate
from analysis.lab_ai.web import headlines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True)
    ap.add_argument("--trades", required=True)
    ap.add_argument("--cfg-contains", action="append", default=[])
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--models", default="qwen3:8b")
    ap.add_argument("--think", choices=["on", "off"], default="on")
    ap.add_argument("--think-tokens", type=int, default=2000)
    ap.add_argument("--web", action="store_true", help="recent trades only; old ones are skipped to avoid hindsight")
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--root", default="data/lake")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    a = ap.parse_args()
    bad = [m for m in a.models.split(",") if m.endswith(":cloud")]
    if bad:
        raise SystemExit(f"refusing cloud models {bad}: local models only")
    df = pd.read_parquet(a.trades)
    for sub in a.cfg_contains:
        df = df[df.cfg.str.contains(sub, regex=False)]
    if df.empty:
        raise SystemExit("no trades match")
    population = df
    rng = np.random.default_rng(a.seed)
    idx = np.sort(rng.choice(len(df), size=min(a.n, len(df)), replace=False))
    sample = df.iloc[idx].reset_index(drop=True)
    si, sn = map(int, a.shard.split("/"))
    sample = sample.iloc[si::sn].reset_index(drop=True)
    res_path = Path("data/lab/ai") / f"{a.name}.jsonl"
    res_path.parent.mkdir(parents=True, exist_ok=True)
    Path("status").mkdir(exist_ok=True)
    status_path = Path("status") / f"ai_{a.name}.json"
    done = set()
    rows = []
    if res_path.exists():
        for l in res_path.read_text().splitlines():
            try:
                r = json.loads(l)
                rows.append(r)
                if r.get("ok"):
                    done.add((r["key"], r["model"]))
            except json.JSONDecodeError:
                pass
    pop_exp = float(population.r_net.mean())
    t0, wall = time.time(), []
    models = [m for m in a.models.split(",") if m]
    total = len(sample) * len(models)
    finished = len(done)

    def save(state, model, stage):
        cmp = {m: gate.compare([r for r in rows if r.get("model") == m]) for m in models}
        st = {"name": a.name, "title": f"AI gate: {a.cfg_contains[0] if a.cfg_contains else 'all'}", "kind": "gate",
              "state": state, "stage": stage, "model": model, "think": a.think == "on", "done": finished, "total": total,
              "elapsed_s": time.time() - t0, "avg_s": float(np.mean(wall)) if wall else None, "results": str(res_path),
              "population_n": int(len(population)), "population_exp_r": pop_exp,
              "population_win": float((population.r_net > 0).mean()), "sample_n": int(len(sample)), "compare": cmp,
              "beat": time.time()}
        tmp = status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(status_path)

    print(f"{len(population)} trades match; sample {len(sample)}; models {models}", flush=True)
    for mi, model in enumerate(models, 1):
        save("running", model, f"{mi}/{len(models)}")
        for i, row in sample.iterrows():
            key = f"{row['cfg']}|{row['symbol']}|{int(row['entry_t'])}"
            if (key, model) in done:
                continue
            ctx = gate.pretrade_context(row, a.root)
            news = []
            if a.web:
                news, _ = headlines(row["symbol"], pd.to_datetime(int(row["entry_t"]), unit="ms").to_pydatetime())
            r = gate.decide(ctx, model, think=a.think == "on", think_tokens=a.think_tokens, news=news, host=a.host)
            r.update(key=key, strategy=row["strategy"], symbol=row["symbol"], side=int(row["side"]),
                     r_net=float(row["r_net"]), r_gross=float(row["r_gross"]), entry_t=int(row["entry_t"]))
            rows.append(r)
            with open(res_path, "a") as f:
                f.write(json.dumps(r) + "\n")
            if r["ok"]:
                done.add((key, model))
                finished += 1
            wall.append(r["wall_s"])
            wall[:] = wall[-30:]
            print(f"[{model} {finished}/{total}] {row['symbol']:9} {r.get('decision')} {r.get('score')} {r['wall_s']:.0f}s r_net={row['r_net']:+.2f}", flush=True)
            save("running", model, f"{mi}/{len(models)}")
    save("done", "-", "all")
    print("gate complete", flush=True)


if __name__ == "__main__":
    main()
