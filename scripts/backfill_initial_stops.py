"""Fill paper_trades.initial_stop_price for swing trades closed before the column existed (21 Oct 2026: closed-trade R
was divided by the ladder-moved stop). A swing trade opened with stop = price - side x |signal price - signal stop|
(scheduler/runner._open_swing_signals), so the original stop comes back from the signal that opened it: same coin and
strategy, logged up to 30 minutes before the trade opened, and taken (no skip reason).

    python -m scripts.backfill_initial_stops [--dry-run]

Backs up paper_trades to ~/backups first; only fills rows still at 0; deletes nothing.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def original_stop(trade, signals) -> float | None:
    """The stop a swing trade opened with, from its signal; None when no signal matches."""
    side = 1 if trade.side == "long" else -1
    opened = trade.opened_at.replace(tzinfo=None) if trade.opened_at.tzinfo else trade.opened_at
    best = None
    for s in signals:
        ts = s.timestamp.replace(tzinfo=None) if s.timestamp.tzinfo else s.timestamp
        if (s.symbol.lower() == trade.symbol.lower() and s.signal_type == trade.signal_type and not (s.skip_reason or "")
                and opened - timedelta(minutes=30) <= ts <= opened + timedelta(minutes=1)):
            if best is None or ts > best[0]:
                best = (ts, s)
    if best is None:
        return None
    s = best[1]
    dist = abs(s.current_price - s.stop_loss)
    base = trade.signal_price or trade.entry_price
    return base - side * dist if dist > 0 else None


async def backfill(dry_run: bool = False, backup_dir: Path | None = None) -> list[dict]:
    from sqlalchemy import or_, select, update

    from storage.database import AsyncSessionFactory
    from storage.models import CryptoSignalLog, PaperTrade
    from analysis.strategy_registry import trade_r
    out = []
    async with AsyncSessionFactory() as s:
        trades = (await s.execute(select(PaperTrade))).scalars().all()
        if not dry_run:
            bdir = backup_dir or (Path.home() / "backups")
            bdir.mkdir(parents=True, exist_ok=True)
            with gzip.open(bdir / f"paper_trades_{datetime.now(UTC):%Y%m%d_%H%M}.jsonl.gz", "wt") as f:
                for t in trades:
                    f.write(json.dumps({c.name: getattr(t, c.name) for c in PaperTrade.__table__.columns}, default=str) + "\n")
        todo = [t for t in trades if t.trade_mode == "swing" and not (t.initial_stop_price or 0)]
        sigs = (await s.execute(select(CryptoSignalLog).where(CryptoSignalLog.trade_mode == "swing"))).scalars().all()
        for t in todo:
            stop = original_stop(t, sigs)
            old_r = trade_r(t)
            row = {"id": t.id, "symbol": t.symbol, "signal_type": t.signal_type, "stop_at_close": t.stop_price,
                   "original_stop": stop, "r_before": round(old_r, 3)}
            if stop is not None:
                t.initial_stop_price = stop
                row["r_after"] = round(trade_r(t), 3)
                if not dry_run:
                    await s.execute(update(PaperTrade).where(PaperTrade.id == t.id, or_(
                        PaperTrade.initial_stop_price.is_(None), PaperTrade.initial_stop_price == 0))
                        .values(initial_stop_price=stop))
            out.append(row)
        if not dry_run:
            await s.commit()
        else:
            await s.rollback()
    return out


if __name__ == "__main__":
    for r in asyncio.run(backfill(dry_run="--dry-run" in sys.argv)):
        print(r)
