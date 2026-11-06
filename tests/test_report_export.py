"""Same inputs must export the same backtest and signal-audit metrics."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from analysis.paper_trading import CycleConfig
from analysis.report_export import (
    STRATEGY_NAME,
    STRATEGY_VERSION,
    build_report,
    metrics_of,
    synthetic_candles,
    write_report,
)

ROOT = Path(__file__).resolve().parents[1]
RUN_AT = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
OTHER_AT = datetime(2026, 11, 6, 13, 0, tzinfo=UTC)
_PROVENANCE = (
    "strategy_name",
    "strategy_version",
    "params",
    "data_range",
    "data_source",
    "random_seed",
    "git_sha",
    "run_timestamp",
)


def _signals():
    from types import SimpleNamespace
    base = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    return [
        SimpleNamespace(
            id=2, symbol="ethusdt", signal_type="confluence", direction="long",
            timeframe="30m", confidence=0.8, current_price=1000.0, target_price=1010.0,
            stop_loss=995.0, outcome="won", pnl_pct=0.8, timestamp=base,
        ),
        SimpleNamespace(
            id=1, symbol="btcusdt", signal_type="volume_spike", direction="short",
            timeframe="15m", confidence=0.7, current_price=62000.0, target_price=61500.0,
            stop_loss=62300.0, outcome="lost", pnl_pct=-0.4, timestamp=base,
        ),
    ]


def _config() -> CycleConfig:
    return CycleConfig(starting_wallet=1000, leverage=10, min_confidence=0.60)


def _report(seed: int = 7, run_at: datetime = RUN_AT, n: int = 400):
    candles = synthetic_candles(n, seed)
    return build_report(
        candles=candles,
        signals=_signals(),
        config=_config(),
        symbol="BTCUSDT",
        seed=seed,
        data_source=f"synthetic:BTCUSDT:n={n}",
        git_sha="abc123",
        run_at=run_at,
    )


def test_repeat_run_matches_metrics_and_provenance():
    first = _report()
    second = _report()
    assert metrics_of(first) == metrics_of(second)
    assert first == second
    prov = first["provenance"]
    for key in _PROVENANCE:
        assert key in prov
    assert prov["strategy_name"] == STRATEGY_NAME
    assert prov["strategy_version"] == STRATEGY_VERSION
    assert prov["random_seed"] == 7
    assert prov["git_sha"] == "abc123"
    assert prov["run_timestamp"] == RUN_AT.isoformat()
    assert prov["data_range"]["from"]
    assert prov["data_range"]["to"]
    assert prov["params"]["starting_wallet"] == 1000
    assert first["metrics"]["backtest"]["trades"]["total"] >= 0
    assert first["metrics"]["signal_audit"]["totals"]["n"] == 2


def test_a_later_clock_does_not_change_metrics():
    first = _report(run_at=RUN_AT)
    later = _report(run_at=OTHER_AT)
    assert metrics_of(first) == metrics_of(later)
    assert first["provenance"]["run_timestamp"] != later["provenance"]["run_timestamp"]


def test_the_seed_changes_the_bars_and_the_same_seed_does_not():
    assert synthetic_candles(50, 1)[10].close != synthetic_candles(50, 2)[10].close
    first_closes = [bar.close for bar in synthetic_candles(50, 4)]
    second_closes = [bar.close for bar in synthetic_candles(50, 4)]
    assert first_closes == second_closes
    assert metrics_of(_report(seed=1, n=200)) == metrics_of(_report(seed=1, n=200))


def test_cli_writes_json_csv_and_markdown_and_repeats(tmp_path: Path):
    signals = tmp_path / "signals.json"
    signals.write_text(json.dumps([
        {
            "id": 1, "symbol": "btcusdt", "signal_type": "confluence", "direction": "long",
            "timeframe": "1h", "confidence": 0.75, "current_price": 100.0,
            "target_price": 102.0, "stop_loss": 99.0, "outcome": "won", "pnl_pct": 1.5,
            "timestamp": "2024-01-02T00:00:00+00:00",
        }
    ]), encoding="utf-8")
    first_dir = tmp_path / "a"
    second_dir = tmp_path / "b"
    for directory in (first_dir, second_dir):
        completed = subprocess.run(
            [
                sys.executable, "-m", "scripts.export_report",
                "--synthetic", "120", "--seed", "7", "--symbol", "BTCUSDT",
                "--min-confidence", "0.6", "--signals", str(signals),
                "--out", str(directory), "--run-at", RUN_AT.isoformat(),
                "--git-sha", "abc123",
            ],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )
        assert completed.returncode == 0
    for name in ("report.json", "report.csv", "report.md"):
        assert (first_dir / name).read_bytes() == (second_dir / name).read_bytes()
    payload = json.loads((first_dir / "report.json").read_text(encoding="utf-8"))
    text = (first_dir / "report.md").read_text(encoding="utf-8")
    csv_text = (first_dir / "report.csv").read_text(encoding="utf-8")
    assert "paper-cycle" in text
    assert "abc123" in text
    assert "random_seed" in csv_text or "provenance.random_seed" in csv_text
    assert payload["provenance"]["random_seed"] == 7
    assert payload["metrics"]["signal_audit"]["totals"]["n"] == 1
    # Library writer and the CLI agree on a second in-process write.
    again = tmp_path / "c"
    write_report(again, payload)
    assert (again / "report.json").read_bytes() == (first_dir / "report.json").read_bytes()
