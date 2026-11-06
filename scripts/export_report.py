"""Export a backtest and a signal audit to JSON, CSV and Markdown.

    python -m scripts.export_report --synthetic 400 --seed 7 --out data/reports/run1
    python -m scripts.export_report --candles bars.csv --signals rows.json --out out/

Same inputs (bars or --synthetic with the same --seed, the same signal file,
the same --run-at and --git-sha) write the same metrics. Omit --run-at and
the timestamp changes; the metrics do not.

Does not place orders or call an exchange. Candles come from a file or from
the local synthetic generator.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from analysis.paper_trading import CycleConfig
from analysis.report_export import (
    build_report,
    load_candles_csv,
    load_signal_rows,
    synthetic_candles,
    write_report,
)


def git_sha(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.strip() or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="Directory for the three report files")
    parser.add_argument("--candles", help="CSV with ts,open,high,low,close,volume")
    parser.add_argument("--signals", help="JSON list of signal rows")
    parser.add_argument(
        "--synthetic", type=int, default=0,
        help="Generate this many 1m bars instead of --candles")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--source", default="", help="Label stored as data_source")
    parser.add_argument("--run-at", default="", help="ISO timestamp. Default: now, UTC")
    parser.add_argument("--git-sha", default="", help="Default: git rev-parse HEAD")
    parser.add_argument("--starting-wallet", type=float, default=1000.0)
    parser.add_argument("--leverage", type=float, default=10.0)
    parser.add_argument("--min-confidence", type=float, default=0.70)
    args = parser.parse_args(argv)

    if args.synthetic and args.candles:
        parser.error("pass either --synthetic or --candles, not both")
    if not args.synthetic and not args.candles and not args.signals:
        parser.error("pass --synthetic, --candles, or --signals")

    candles = None
    if args.synthetic:
        candles = synthetic_candles(args.synthetic, args.seed)
        source = args.source or f"synthetic:{args.symbol.upper()}:n={args.synthetic}"
    elif args.candles:
        candles = load_candles_csv(Path(args.candles))
        source = args.source or f"csv:{args.candles}"
    else:
        source = args.source or "signals-only"

    signals = load_signal_rows(Path(args.signals)) if args.signals else None
    run_at = datetime.fromisoformat(args.run_at) if args.run_at else None
    config = CycleConfig(
        starting_wallet=args.starting_wallet,
        leverage=args.leverage,
        min_confidence=args.min_confidence,
    )
    report = build_report(
        candles=candles,
        signals=signals,
        config=config,
        symbol=args.symbol.upper(),
        seed=args.seed,
        data_source=source,
        git_sha=git_sha(args.git_sha or None),
        run_at=run_at,
    )
    paths = write_report(Path(args.out), report)
    json.dump({name: str(path) for name, path in paths.items()}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
