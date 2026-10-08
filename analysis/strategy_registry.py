"""
Strategy registry: which swing strategies may trade, since when, and the drawdown alarm
(docs/INTEGRATION_PLAN.md 2.3, 2.4; O6).

* Every spec in `swing_strategies` gets a row keyed "4h@vol_breakout", with a digest of its code, params and
  timeframe. A changed digest resets `live_from`: results of the old code are not evidence for the new one.
* `live_from` is stamped at the spec's first paper trade. Forward R after it is the only clean evidence about the
  swing book (section 0 of the plan: everything else was chosen on the same history it is tested on).
* Only incubating / trusted specs open trades. The drawdown alarm pauses a spec whose forward results fall
  `limit_r` R below their peak, and the runner sends one Telegram alert. The owner un-pauses on purpose.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select

from storage.models import StrategyRegistry

TRADING = ("incubating", "trusted")

# Worst peak-to-trough fall in R per spec over the 5-year backtest (filter on, BCH/LTC out; data/lab/runs/filt_vol_*,
# 7 Oct 2026). Forward results falling further than the spec ever did in testing is the alarm.
BACKTEST_MAX_DD_R = {"4h@donchian": 28.7, "4h@ichimoku": 20.0, "4h@keltner_break": 24.7, "4h@vol_breakout": 13.8,
                     "8h@donchian": 9.7, "8h@ichimoku": 12.8, "8h@keltner_break": 12.0, "8h@vol_breakout": 8.6}
DEFAULT_LIMIT_R = 15.0


def alarm_limit(key: str, factor: float = 1.0) -> float:
    return factor * BACKTEST_MAX_DD_R.get(key, DEFAULT_LIMIT_R)


def spec_key(strategy_id: str, timeframe: str) -> str:
    return f"{timeframe}@{strategy_id}"


def key_of_signal(signal_type: str, timeframe: str) -> str:
    """'swing_vol_breakout' on '4h' -> '4h@vol_breakout'."""
    return spec_key(signal_type.removeprefix("swing_"), timeframe)


def spec_digest(strategy_id: str, timeframe: str, params: dict) -> str:
    from analysis.lab.ledger import code_digest
    raw = json.dumps([code_digest(strategy_id), timeframe, sorted(params.items())], default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def trade_r(t) -> float:
    """A closed paper trade in R, net of fees and funding: the same yardstick as the backtest."""
    side = 1 if t.side == "long" else -1
    risk = abs(t.entry_price - t.stop_price) / t.entry_price if t.entry_price else 0
    move = side * (t.exit_price - t.entry_price) / t.entry_price if t.entry_price else 0
    cost = (t.trading_fees + t.funding_paid) / (t.margin * t.leverage) if t.margin and t.leverage else 0
    return (move - cost) / risk if risk > 0 else 0.0


def drawdown_r(rs: list[float]) -> float:
    """Largest fall of cumulative R from its peak (0 = never below a previous high)."""
    cum = peak = worst = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def forward(trades: list, row) -> list[float]:
    """R of this spec's closed paper trades since its live_from, oldest first."""
    if row is None or row.live_from is None:
        return []
    lf = row.live_from.replace(tzinfo=None)
    return [trade_r(t) for t in sorted(trades, key=lambda t: t.closed_at)
            if key_of_signal(t.signal_type or "", t.timeframe or "") == row.spec_key
            and t.opened_at is not None and t.opened_at.replace(tzinfo=None) >= lf]


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def sync(session, specs: list[tuple[str, str, dict]]) -> list[str]:
    """Upsert one row per (timeframe, strategy, params). Returns notes on new specs and code/param changes."""
    rows = {r.spec_key: r for r in (await session.execute(select(StrategyRegistry))).scalars()}
    notes = []
    for tf, sid, params in specs:
        key, dig = spec_key(sid, tf), spec_digest(sid, tf, params)
        p = ",".join(f"{k}={v}" for k, v in sorted(params.items()))
        row = rows.get(key)
        if row is None:
            session.add(StrategyRegistry(spec_key=key, params=p, digest=dig, status="incubating", live_from=None,
                                         note="registered", created_at=_now(), updated_at=_now()))
            notes.append(f"{key}: registered (incubating)")
        elif row.digest != dig:
            row.digest, row.params, row.live_from, row.updated_at = dig, p, None, _now()
            row.note = "code or params changed: forward clock restarts at the next trade"
            notes.append(f"{key}: changed, live_from reset")
    await session.commit()
    return notes


async def load(session) -> dict[str, StrategyRegistry]:
    return {r.spec_key: r for r in (await session.execute(select(StrategyRegistry))).scalars()}


async def stamp_live_from(session, key: str, when: datetime) -> None:
    row = (await session.execute(select(StrategyRegistry).where(StrategyRegistry.spec_key == key))).scalar_one_or_none()
    if row is not None and row.live_from is None:
        row.live_from, row.updated_at = when.replace(tzinfo=None), _now()
        row.note = "forward clock started"
        await session.commit()


async def check_alarms(session, trades: list, factor: float = 1.0) -> list[str]:
    """Pause every trading spec whose forward drawdown reached factor x its backtest worst. One message per pause."""
    msgs = []
    for row in (await session.execute(select(StrategyRegistry))).scalars():
        if row.status not in TRADING:
            continue
        rs = forward(trades, row)
        dd = drawdown_r(rs)
        limit_r = alarm_limit(row.spec_key, factor)
        if factor > 0 and dd >= limit_r:
            row.status, row.updated_at = "paused", _now()
            row.note = f"drawdown alarm: {dd:.1f}R below peak after {len(rs)} forward trades (limit {limit_r:g}R)"
            msgs.append(f"{row.spec_key} paused: {row.note}")
    if msgs:
        await session.commit()
    return msgs


def forward_t(rs: list[float]) -> float:
    """t-statistic of the forward mean R (0 when it cannot be computed)."""
    import math
    n = len(rs)
    if n < 2:
        return 0.0
    m = sum(rs) / n
    sd = math.sqrt(sum((r - m) ** 2 for r in rs) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd > 0 else 0.0


async def check_promotions(session, trades: list, min_trades: int = 30, t_line: float = 1.645) -> list[str]:
    """Roadmap F-3 / plan B1: incubating -> trusted once the spec has min_trades forward trades and its forward
    mean R is above zero at one-sided 95% (t >= t_line). Trusted is never automatic in the other direction:
    the drawdown alarm pauses, the owner retires."""
    msgs = []
    for row in (await session.execute(select(StrategyRegistry))).scalars():
        if row.status != "incubating":
            continue
        rs = forward(trades, row)
        t = forward_t(rs)
        if len(rs) >= min_trades and t >= t_line:
            row.status, row.updated_at = "trusted", _now()
            row.note = f"promoted: {len(rs)} forward trades, mean {sum(rs) / len(rs):+.2f}R, t {t:.2f}"
            msgs.append(f"{row.spec_key}: {row.note}")
    if msgs:
        await session.commit()
    return msgs
