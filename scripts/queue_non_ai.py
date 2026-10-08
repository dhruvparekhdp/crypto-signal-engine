"""Ordered non-AI lab queue for ThinkPad/Mac. No LLM calls.

    python -m scripts.queue_non_ai              # run every stage in order
    python -m scripts.queue_non_ai --only N1,N2 # subset
    python -m scripts.queue_non_ai --list

Each stage is wrapped in scripts.job so status/*.json and logs/*.log update for the dashboard.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(Path(sys.executable))
LIVE = "vol_breakout,keltner_break,donchian,ichimoku"
# Live paper coins (BCH/LTC excluded — no edge / not traded)
SYMS = "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,ADAUSDT,DOGEUSDT,AVAXUSDT,LINKUSDT,SUIUSDT"
WORKERS = str(max(2, min(10, (os.cpu_count() or 4) - 2)))

# Live parameter overrides (match production swing_book DEFAULT_SPECS)
SET_4H = ["--set", "vol_breakout.z=3.0", "--set", "keltner_break.k=2.5", "--set", "donchian.n=100"]
SET_8H = ["--set", "vol_breakout.z=3.0", "--set", "keltner_break.k=2.0", "--set", "donchian.n=100"]


def job(name: str, argv: list[str]) -> None:
    """Run one tracked job; raise if it fails."""
    cmd = [PY, "-m", "scripts.job", name, "--", PY, "-m", *argv]
    print(f"\n=== JOB {name} ===\n{' '.join(cmd)}\n", flush=True)
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit(f"job {name} failed with {r.returncode}")


STAGES: dict[str, list[tuple[str, list[str]]]] = {
    # N1: live specs + null test (honest random-entry baseline)
    "N1": [
        ("swing_null_4h", [
            "scripts.run_lab", "--strategies", LIVE, "--symbols", SYMS, "--tf", "4h",
            "--exits", "swing", "--cost", "india_gst", "--null-trials", "200",
            "--workers", WORKERS, "--out", "data/lab/runs/swing_null_4h",
            "--ledger-family", "swing_null_4h", "--ledger-kind", "live_rescore",
            "--hypothesis", "live 4h specs beat random under new code", *SET_4H,
        ]),
        ("swing_null_8h", [
            "scripts.run_lab", "--strategies", LIVE, "--symbols", SYMS, "--tf", "8h",
            "--exits", "swing", "--cost", "india_gst", "--null-trials", "200",
            "--workers", WORKERS, "--out", "data/lab/runs/swing_null_8h",
            "--ledger-family", "swing_null_8h", "--ledger-kind", "live_rescore",
            "--hypothesis", "live 8h specs beat random under new code", *SET_8H,
        ]),
    ],
    # N2: same strategies across timeframes (is 4h/8h still best?)
    "N2": [
        (f"tf_sweep_{tf}", [
            "scripts.run_lab", "--strategies", LIVE, "--symbols", SYMS, "--tf", tf,
            "--exits", "swing", "--cost", "india_gst", "--workers", WORKERS,
            "--out", f"data/lab/runs/tf_sweep_{tf}",
            "--ledger-family", f"tf_sweep_{tf}", "--ledger-kind", "search",
            "--hypothesis", f"live strategies on {tf} bars",
            *(SET_8H if tf in ("8h", "12h") else SET_4H),
        ])
        for tf in ("1h", "2h", "4h", "8h", "12h")
    ],
    # N3: exit bake-off on 4h and 8h
    "N3": [
        ("exits_4h", [
            "scripts.run_lab", "--strategies", LIVE, "--symbols", SYMS, "--tf", "4h",
            "--exits", "swing,b5_tight,b5_partial,b5_nnfx,swing_trail",
            "--cost", "india_gst", "--workers", WORKERS, "--out", "data/lab/runs/exits_4h",
            "--ledger-family", "exits_4h", "--ledger-kind", "ablation",
            "--hypothesis", "current swing exit still best on 4h", *SET_4H,
        ]),
        ("exits_8h", [
            "scripts.run_lab", "--strategies", LIVE, "--symbols", SYMS, "--tf", "8h",
            "--exits", "swing,b5_tight,b5_partial,b5_nnfx,swing_trail",
            "--cost", "india_gst", "--workers", WORKERS, "--out", "data/lab/runs/exits_8h",
            "--ledger-family", "exits_8h", "--ledger-kind", "ablation",
            "--hypothesis", "current swing exit still best on 8h", *SET_8H,
        ]),
    ],
    # N4: cost stress
    "N4": [
        ("stress_swing_4h", [
            "scripts.run_lab", "--strategies", LIVE, "--symbols", SYMS, "--tf", "4h",
            "--exits", "swing", "--cost", "stress", "--workers", WORKERS,
            "--out", "data/lab/runs/stress_swing_4h",
            "--ledger-family", "stress_swing_4h", "--ledger-kind", "stress",
            "--hypothesis", "live 4h survives stress costs", *SET_4H,
        ]),
    ],
    # N5: daily BTC/ETH
    "N5": [
        ("daily_trend", [
            "scripts.run_lab", "--strategies", "tsmom,donchian,ichimoku,supertrend",
            "--symbols", "BTCUSDT,ETHUSDT", "--tf", "1d", "--exits", "daily_trend",
            "--cost", "india_gst", "--workers", "4", "--out", "data/lab/runs/daily_trend",
            "--ledger-family", "daily_trend", "--ledger-kind", "search",
            "--hypothesis", "daily momentum on BTC/ETH (few trades expected)",
        ]),
    ],
    # N6/N7 reports (need prior named runs; also work on copied Mac runs)
    "N6": [
        ("wallet_plan", ["scripts.wallet_plan_backtest", "--out", "data/lab/wallet_plan_backtest.json"]),
        ("sizing_backtest", ["scripts.sizing_backtest"]),
    ],
    "N7": [
        ("edge_report", ["scripts.edge_report", "--out", "data/lab/edge_report.json"]),
    ],
    # N8: broader search (ledger-counted) — last, longest
    "N8": [
        ("breakout_grid_4h", [
            "scripts.run_lab", "--family", "breakout,trend,volume", "--symbols", SYMS, "--tf", "4h",
            "--exits", "swing", "--cost", "india_gst", "--grid", "--grid-limit", "4",
            "--workers", WORKERS, "--out", "data/lab/runs/breakout_grid_4h",
            "--ledger-family", "breakout_grid_4h", "--ledger-kind", "search",
            "--hypothesis", "grid search breakout+trend+volume on 4h after live book",
        ]),
    ],
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma list of stages, e.g. N1,N2")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    order = [k for k in ("N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8") if k in STAGES]
    if a.only:
        order = [x.strip() for x in a.only.split(",") if x.strip()]
    if a.list:
        for k in order:
            for name, _ in STAGES[k]:
                print(f"{k:4} {name}")
        return
    Path(ROOT / "status").mkdir(exist_ok=True)
    Path(ROOT / "logs").mkdir(exist_ok=True)
    t0 = time.time()
    done = []
    for stage in order:
        for name, argv in STAGES[stage]:
            job(name, argv)
            done.append(name)
            (ROOT / "status" / "non_ai_queue.json").write_text(
                __import__("json").dumps({"state": "running", "done": done, "stage": stage,
                                          "elapsed_s": time.time() - t0, "host": os.uname().nodename}))
    (ROOT / "status" / "non_ai_queue.json").write_text(
        __import__("json").dumps({"state": "done", "done": done, "elapsed_s": time.time() - t0,
                                  "host": os.uname().nodename}))
    print(f"\nnon-AI queue complete: {len(done)} jobs in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
