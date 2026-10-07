"""Roadmap Q-2: record every configuration the lab scored before the trial ledger existed (family "lab-history").

    python -m scripts.ledger_backfill            # dry run: counts only
    python -m scripts.ledger_backfill --apply

One row per distinct (strategy, params, exit, timeframe) found in data/lab/runs/*; re-running adds nothing (the
digest is the configuration itself). The code digest is unknown for old runs, so the configuration string stands in.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from analysis.lab import ledger

RUNS = Path("data/lab/runs")


def configs() -> dict[str, dict]:
    found = {}
    for d in sorted(RUNS.glob("*")):
        tf = ""
        try:
            tf = json.loads((d / "spec.json").read_text()).get("sig_tf") or ""
        except (OSError, ValueError):
            pass
        cfgs = []
        if (d / "summary.csv").exists():
            cfgs = list(pd.read_csv(d / "summary.csv").cfg.dropna().unique())
        elif (d / "trades.parquet").exists():
            try:
                cfgs = list(pd.read_parquet(d / "trades.parquet", columns=["cfg"]).cfg.unique())
            except Exception:  # noqa: BLE001
                cfgs = []
        for c in cfgs:
            key = f"{c}|tf={tf or 'default'}"
            dig = hashlib.sha256(key.encode()).hexdigest()[:16]
            found.setdefault(dig, {"digest": dig, "cfg": c, "timeframe": tf or "default", "first_run": d.name})
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    found = configs()
    print(f"{len(found)} distinct configurations in {len(list(RUNS.glob('*')))} saved runs")
    if a.apply:
        res = ledger.record(list(found.values()), "lab-history", "search",
                            hypothesis="back-fill: configurations scored before the ledger (Q-2)")
        print(f"recorded: {res['new']} new, family N={res['n_family']}; all search trials: {ledger.trial_count()}")


if __name__ == "__main__":
    main()
