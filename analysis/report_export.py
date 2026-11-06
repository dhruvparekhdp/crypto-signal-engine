"""Deterministic JSON, CSV and Markdown export of a backtest and a signal audit.

The numbers come from the existing engines (`BacktestEngine` and `audit`).
This module only names the run and writes it down. Same candles, same signal
rows, same config and same seed produce the same metrics. The clock and the
git SHA are inputs, so a re-run that passes the same ones is byte-identical.
"""
from __future__ import annotations

import csv
import json
import random
from collections.abc import Sequence
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from analysis.backtest import BacktestEngine
from analysis.paper_trading import CycleConfig
from analysis.signal_audit import audit
from collectors.historical_klines import Candle

# Bump when the paper-cycle replay or the audit arithmetic this export
# reports is no longer the same calculation.
STRATEGY_NAME = "paper-cycle"
STRATEGY_VERSION = "1"

_AUDIT_SLICES = (
    "by_symbol",
    "by_setup",
    "by_horizon",
    "by_direction",
    "by_confidence",
    "by_edge",
)


def synthetic_candles(
    n: int,
    seed: int,
    *,
    start: float = 62_000.0,
    vol: float = 0.004,
    drift: float = 0.0,
    start_ts: datetime | None = None,
) -> list[Candle]:
    """OHLC bars from an isolated RNG. The global `random` state is not used."""
    rng = random.Random(seed)
    t0 = start_ts or datetime(2024, 1, 1, tzinfo=UTC)
    out: list[Candle] = []
    price = start
    for i in range(n):
        opened = price
        closed = opened * (1 + rng.gauss(drift, vol))
        wick = abs(rng.gauss(0, vol * 0.6))
        out.append(Candle(
            ts=t0 + timedelta(minutes=i),
            open=opened,
            high=max(opened, closed) * (1 + wick),
            low=min(opened, closed) * (1 - wick),
            close=closed,
            volume=rng.uniform(80, 400),
        ))
        price = closed
    return out


def to_plain(value: Any) -> Any:
    """JSON-safe copy. Dict keys are sorted so two dumps compare equal."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): to_plain(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    if isinstance(value, float):
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def load_candles_csv(path: Path) -> list[Candle]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        candles: list[Candle] = []
        for row in reader:
            ts = datetime.fromisoformat(row["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            candles.append(Candle(
                ts=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))
    candles.sort(key=lambda candle: candle.ts)
    return candles


def load_signal_rows(path: Path) -> list[SimpleNamespace]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("signals") or payload.get("rows") or []
    rows: list[SimpleNamespace] = []
    for raw in payload:
        item = dict(raw)
        stamp = item.get("timestamp")
        if isinstance(stamp, str) and stamp:
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            item["timestamp"] = parsed
        rows.append(SimpleNamespace(**item))
    rows.sort(key=lambda row: (int(getattr(row, "id", 0) or 0), _stamp(row)))
    return rows


def build_report(
    *,
    candles: Sequence[Candle] | None = None,
    signals: Sequence[Any] | None = None,
    config: CycleConfig | None = None,
    symbol: str = "BTCUSDT",
    seed: int = 0,
    data_source: str = "",
    git_sha: str = "unknown",
    run_at: datetime | None = None,
) -> dict[str, Any]:
    """One report. Metrics depend only on candles, signals, config and symbol."""
    cfg = config or CycleConfig()
    bar_list = list(candles or [])
    row_list = list(signals or [])
    when = run_at or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)

    backtest_metrics: dict[str, Any] | None = None
    if bar_list:
        # The engine does not draw random numbers. The seed is still recorded,
        # and synthetic bars are generated from it before this call.
        result = BacktestEngine(cfg).run(symbol, bar_list)
        summary = result.summary()
        summary["by_signal_type"] = {
            key: summary["by_signal_type"][key]
            for key in sorted(summary["by_signal_type"])
        }
        summary["closed_trades"] = _trades(result)
        backtest_metrics = summary

    audit_metrics: dict[str, Any] | None = None
    if row_list:
        audited = audit(row_list)
        audited["records"] = sorted(
            audited["records"], key=lambda row: (row["id"], row["timestamp"] or ""))
        for slice_name in _AUDIT_SLICES:
            audited[slice_name] = sorted(
                audited[slice_name], key=lambda row: (str(row.get("key")), str(row.get("label"))))
        audit_metrics = audited

    report = {
        "provenance": {
            "strategy_name": STRATEGY_NAME,
            "strategy_version": STRATEGY_VERSION,
            "params": to_plain(cfg),
            "data_range": _data_range(bar_list, row_list),
            "data_source": data_source,
            "random_seed": int(seed),
            "git_sha": git_sha,
            "run_timestamp": when.isoformat(),
        },
        "metrics": {
            "backtest": backtest_metrics,
            "signal_audit": audit_metrics,
        },
    }
    return report


def write_report(directory: Path, report: dict[str, Any]) -> dict[str, Path]:
    """Write report.json, report.csv and report.md. Keys are sorted."""
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "report.json"
    csv_path = directory / "report.csv"
    md_path = directory / "report.md"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    _write_csv(csv_path, report)
    md_path.write_text(_markdown(report), encoding="utf-8")
    return {"json": json_path, "csv": csv_path, "markdown": md_path}


def metrics_of(report: dict[str, Any]) -> dict[str, Any]:
    return report["metrics"]


def _trades(result: Any) -> list[dict[str, Any]]:
    rows = []
    for trade in result.trades:
        rows.append({
            "closed_at": trade.closed_at.isoformat(),
            "reason": trade.reason.value,
            "side": trade.position.side.value,
            "signal_type": trade.position.signal_type or "",
            "net_pnl": round(trade.net_pnl, 6),
            "fees_paid": round(trade.fees_paid, 6),
            "wallet_after": round(trade.wallet_after, 6),
        })
    rows.sort(key=lambda row: (row["closed_at"], row["signal_type"], row["net_pnl"], row["side"]))
    return rows


def _stamp(row: Any) -> str:
    ts = getattr(row, "timestamp", None)
    if isinstance(ts, datetime):
        return ts.isoformat()
    return "" if ts is None else str(ts)


def _data_range(candles: Sequence[Candle], signals: Sequence[Any]) -> dict[str, str | None]:
    stamps: list[datetime] = []
    if candles:
        stamps.append(candles[0].ts)
        stamps.append(candles[-1].ts)
    for row in signals:
        ts = getattr(row, "timestamp", None)
        if isinstance(ts, datetime):
            stamps.append(ts)
    if not stamps:
        return {"from": None, "to": None}
    return {"from": min(stamps).isoformat(), "to": max(stamps).isoformat()}


def _flatten(prefix: str, value: Any, out: list[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            _flatten(child, value[key], out)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _flatten(f"{prefix}[{index}]", item, out)
        return
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    out.append((prefix, text))


def _write_csv(path: Path, report: dict[str, Any]) -> None:
    rows: list[tuple[str, str]] = []
    _flatten("", report, rows)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["key", "value"])
        writer.writerows(rows)


def _markdown(report: dict[str, Any]) -> str:
    prov = report["provenance"]
    lines = [
        f"# {prov['strategy_name']} {prov['strategy_version']}",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Strategy | {_cell(prov['strategy_name'])} {_cell(prov['strategy_version'])} |",
        "| Data range | "
        f"{_cell(prov['data_range']['from'])} .. {_cell(prov['data_range']['to'])} |",
        f"| Data source | {_cell(prov['data_source'])} |",
        f"| Random seed | {prov['random_seed']} |",
        f"| Git SHA | {_cell(prov['git_sha'])} |",
        f"| Run timestamp | {_cell(prov['run_timestamp'])} |",
        "",
    ]
    backtest = report["metrics"]["backtest"]
    if backtest:
        wallet = backtest["wallet"]
        trades = backtest["trades"]
        lines.extend([
            "## Backtest",
            "",
            f"- Symbol: {backtest['symbol']}",
            f"- Candles: {backtest['period']['candles']}",
            f"- Net P&L: {wallet['net_pnl']}",
            f"- Return %: {wallet['return_pct']}",
            f"- Trades: {trades['total']}  wins: {trades['wins']}  "
            f"win rate %: {trades['win_rate_pct']}",
            f"- Expectancy per trade: {trades['expectancy_per_trade']}",
            f"- Max drawdown %: {trades['max_drawdown_pct']}",
            "",
        ])
    audited = report["metrics"]["signal_audit"]
    if audited:
        totals = audited["totals"]
        lines.extend([
            "## Signal audit",
            "",
            f"- Signals: {totals['n']}  decided: {totals['decided']}",
            f"- Hit rate %: {totals['hit_rate_pct']}",
            f"- Net hit rate %: {totals['net_hit_rate_pct']}",
            f"- Expectancy %: {totals['expectancy_pct']}",
            "",
        ])
    return "\n".join(lines)


def _cell(value: Any) -> str:
    return str("" if value is None else value).replace("|", "\\|")
