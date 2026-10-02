"""Long-running, resumable review queue with local Ollama models in their heaviest configuration.

Stages run model by model (so Ollama loads each model once). Results are appended to a JSONL as they
finish and progress is written to status/ai_<name>.json for the dashboard.

    python -m scripts.ai_queue --name extreme --hist data/lab/runs/cand_4h_tp/trades.parquet --recent data/lab/runs/recent/trades.parquet
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from analysis.lab_ai import context, review
from analysis.lab_ai.web import headlines

MODELS = [("llama3.2:3b", None, 400), ("qwen3:8b", True, 700), ("deepseek-r1:8b", True, 700)]


def pick_hist(df, n):
    d = df[df.cfg.str.startswith("vol_breakout")].sort_values("r_net")
    if d.empty:
        d = df.sort_values("r_net")
    third = max(1, n // 3)
    mid = d.iloc[third:-third] if len(d) > 3 * third else d
    rnd = mid.sample(min(third, len(mid)), random_state=5) if len(mid) else mid
    return pd.concat([d.head(third), d.tail(third), rnd]).drop_duplicates(["cfg", "symbol", "entry_t"])


def pick_recent(df, n, hours):
    end = int(df.entry_t.max())
    d = df[df.entry_t >= end - hours * 3_600_000]
    rng = np.random.default_rng(9)
    out, groups = [], {k: g.sample(frac=1, random_state=int(rng.integers(1e9))) for k, g in d.groupby("strategy")}
    i = 0
    while len(out) < min(n, len(d)) and i < 5000:
        for k in groups:
            if i < len(groups[k]) and len(out) < n:
                out.append(groups[k].iloc[i])
        i += 1
    return pd.DataFrame(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="extreme")
    ap.add_argument("--hist", required=True)
    ap.add_argument("--recent", required=True)
    ap.add_argument("--hist-n", type=int, default=60)
    ap.add_argument("--recent-n", type=int, default=30)
    ap.add_argument("--recent-hours", type=float, default=48)
    ap.add_argument("--think", choices=["auto", "off"], default="auto", help="off: no thinking for models that support turning it off")
    ap.add_argument("--models", default="", help="comma list; default all three")
    ap.add_argument("--root", default="data/lake")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    a = ap.parse_args()
    out_dir = Path("data/lab/ai")
    out_dir.mkdir(parents=True, exist_ok=True)
    res_path = out_dir / f"{a.name}.jsonl"
    status_path = Path("status") / f"ai_{a.name}.json"
    Path("status").mkdir(exist_ok=True)

    sets = {"hist": pick_hist(pd.read_parquet(a.hist), a.hist_n).reset_index(drop=True),
            "recent": (pick_recent(pd.read_parquet(a.recent), a.recent_n, a.recent_hours).reset_index(drop=True)
                       if a.recent_n else pd.DataFrame())}
    print({k: len(v) for k, v in sets.items()}, flush=True)
    done = set()
    if res_path.exists():
        for l in res_path.read_text().splitlines():
            try:
                r = json.loads(l)
                if r.get("ok"):
                    done.add((r["set"], r["key"], r["model"]))
            except (json.JSONDecodeError, KeyError):
                pass
    want = [m for m in a.models.split(",") if m]
    stages = [(m, th, npred, sn) for (m, th, npred) in MODELS if not want or m in want
              for sn in ("hist", "recent") if len(sets[sn])]
    ctx_cache = {}
    t_start = time.time()
    total_all = sum(len(sets[sn]) for _, _, _, sn in stages)
    finished = len(done)
    wall = []

    def status(state, stage, model, think, d, tot):
        st = {"name": a.name, "title": "Extreme local-model review", "state": state, "stage": stage, "model": model,
              "think": think, "done": d, "total": tot, "elapsed_s": time.time() - t_start,
              "avg_s": float(np.mean(wall)) if wall else None, "overall_done": finished, "overall_total": total_all,
              "results": str(res_path), "beat": time.time()}
        tmp = status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(status_path)

    for si, (model, think, npred, sn) in enumerate(stages, 1):
        if a.think == "off" and think:
            think, npred = False, 350
        df = sets[sn]
        todo = [(i, r) for i, r in df.iterrows() if (sn, f"{r['cfg']}|{r['symbol']}|{int(r['entry_t'])}", model) not in done]
        d0 = len(df) - len(todo)
        status("running", f"{si}/{len(stages)} {sn}", model, think, d0, len(df))
        for n, (i, row) in enumerate(todo, 1):
            key = f"{row['cfg']}|{row['symbol']}|{int(row['entry_t'])}"
            if key not in ctx_cache:
                ctx_cache[key] = context.build(row, a.root, extended=True)
            c = ctx_cache[key]
            news = []
            if sn == "recent":
                news, _ = headlines(row["symbol"], pd.to_datetime(int(row["entry_t"]), unit="ms").to_pydatetime())
            r = review.review_x(c, model, think, num_predict=npred, news=news, host=a.host, key=key)
            r.update(set=sn, key=key, strategy=row["strategy"], symbol=row["symbol"], r_net=float(row["r_net"]),
                     news_n=len(news), **review.grade(c, r))
            with open(res_path, "a") as f:
                f.write(json.dumps(r) + "\n")
            wall.append(r["wall_s"])
            wall[:] = wall[-30:]
            finished += 1
            print(f"[{model} {sn} {d0 + n}/{len(df)}] {row['strategy']:16} {r['wall_s']:6.1f}s {r.get('cause')} ok={r['ok']}", flush=True)
            status("running", f"{si}/{len(stages)} {sn}", model, think, d0 + n, len(df))
    status("done", "all", "-", None, total_all, total_all)
    print("queue complete", flush=True)


if __name__ == "__main__":
    main()
