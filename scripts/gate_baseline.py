"""Walk-forward non-AI filter on a work set's population: the bar the LLM has to beat.

    python -m scripts.gate_baseline --name gate_main
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from analysis.lab_ai import baseline


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    a = ap.parse_args()
    d = Path(os.environ.get("LAB_ROOT", ".")) / "work" / a.name
    rows = [json.loads(l) for l in open(d / "population.jsonl")]
    scored = baseline.walk_forward(rows)
    full, sample = baseline.summarize(scored), baseline.summarize([r for r in scored if r["in_sample"]])
    out = {"population_walk_forward": full, "same_trades_as_the_ai_saw": sample}
    (d / "baseline.json").write_text(json.dumps(out))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
