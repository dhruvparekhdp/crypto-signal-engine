"""
Trial ledger: one row per configuration the lab, v2 or a null test ever scored (docs/INTEGRATION_PLAN.md Q2, Q5).

Why: a p-value means nothing without knowing how many things were tried to find it. Every run appends its
configurations here, keyed by a digest of exactly what was tested:

    strategy code (its own function + features/simulate/costs, the files that decide its trades), params,
    timeframe, exit model, window, cost preset and a fingerprint of the data (per symbol: kline files and sizes).

N = distinct digests with kind == "search" in a family. Re-running the same question adds no new trial; changing
any input (even the data) does. Stress tests, ablations and live re-scores are recorded but not counted.

Stored as append-only JSON lines in the repo (audit/trial_ledger.jsonl), not in the production database: lab runs
happen on the Mac and the home laptops, the production database stays read-only, and git keeps the history.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "audit" / "trial_ledger.jsonl"
CODE_FILES = ("analysis/lab/features.py", "analysis/lab/simulate.py", "analysis/lab/costs.py")
KINDS = ("search", "stress", "ablation", "live_rescore")


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def code_digest(strategy_id: str) -> str:
    """The strategy's own function plus the shared files that turn its signals into trades."""
    from analysis.lab.strategies import REGISTRY
    st = REGISTRY[strategy_id]
    try:
        src = inspect.getsource(st.fn)
    except (OSError, TypeError):
        src = repr(st.fn)
    shared = "".join((ROOT / f).read_text() for f in CODE_FILES if (ROOT / f).exists())
    return _h(f"{strategy_id}\n{src}\n{json.dumps(st.defaults, sort_keys=True)}\n{shared}")


def data_fingerprint(symbols: list[str], root: str, market: str = "um") -> str:
    """Kline file names and sizes per symbol: changes when data is added, removed or rewritten."""
    parts = []
    for s in sorted(symbols):
        base = Path(root) / market / "klines" / s
        files = sorted(base.glob("*/*.parquet")) if base.exists() else []
        parts.append(s + ":" + ",".join(f"{f.parent.name}/{f.name}={f.stat().st_size}" for f in files))
    return _h("\n".join(parts))


def trial_digest(strategy: str, params: dict, timeframe: str, exit_key: str, window: tuple, cost: str,
                 data_fp: str, code: str | None = None) -> str:
    code = code or code_digest(strategy)
    return _h(json.dumps([strategy, code, sorted(params.items()), timeframe, exit_key, list(window), cost, data_fp],
                         default=str))


def load(path: Path | None = None) -> list[dict]:
    path = path or LEDGER
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def trial_count(family: str | None = None, kind: str = "search", path: Path | None = None) -> int:
    """N: distinct configurations tried (only `kind` rows, default search) in a family, or in all families."""
    return len({r["digest"] for r in load(path)
                if r.get("kind") == kind and (family is None or r.get("family") == family)})


def record(rows: list[dict], family: str, kind: str = "search", hypothesis: str = "", proposed_by: str = "owner",
           parent_id: str | None = None, path: Path | None = None) -> dict:
    """Append trials. Each row needs `digest`; anything else (cfg, expectancy_r, null_p...) is kept as given.
    Returns how many were new to the family and the family's N after recording."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    path = path or LEDGER
    seen = {r["digest"] for r in load(path) if r.get("family") == family and r.get("kind") == kind}
    run_id = _h(f"{time.time()}{family}{len(rows)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    new = 0
    with path.open("a") as f:
        for r in rows:
            new += r["digest"] not in seen
            seen.add(r["digest"])
            f.write(json.dumps({"ts": int(time.time()), "run_id": run_id, "family": family, "kind": kind,
                                "hypothesis": hypothesis, "proposed_by": proposed_by, "parent_id": parent_id,
                                "disposition": r.pop("disposition", None), **r}, default=str) + "\n")
    return {"run_id": run_id, "new": new, "n_family": trial_count(family, kind, path)}
