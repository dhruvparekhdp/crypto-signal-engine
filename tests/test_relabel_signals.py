"""Re-labelling past signals (19 Oct 2026): back up the whole log first, change only rows that still hold the label
the new one was computed from, delete nothing."""
import gzip
import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_apply_backs_up_then_updates_only_unchanged_rows(tmp_path, monkeypatch):
    from scripts import relabel_signals
    from storage.models import Base, CryptoSignalLog
    from storage.repository import Repository
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        for _ in range(3):
            await Repository(s).log_crypto_signal(symbol="ethusdt", signal_type="confluence", direction="long",
                                                  trigger_description="x", confidence=0.6, current_price=100.0,
                                                  target_price=102.0, stop_loss=99.0, edge_pct=0, stake_pct=0,
                                                  timeframe="81m")
        rows = (await Repository(s).crypto_signals_between(1, 0))
        ids = sorted(r.id for r in rows)
        for r, out in zip(sorted(rows, key=lambda r: r.id), ("won", "won", "lost")):
            r.outcome = out
        await s.commit()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"changes": [
        {"id": ids[0], "old": "won", "new": "expired", "new_pnl": 0.3},
        {"id": ids[1], "old": "pending", "new": "lost", "new_pnl": -1.0},       # the live job changed it since
    ]}))
    monkeypatch.setattr("storage.database.AsyncSessionFactory", sm)
    monkeypatch.setenv("HOME", str(tmp_path))
    await relabel_signals.apply(str(plan))
    async with sm() as s:
        got = {r.id: (r.outcome, r.pnl_pct) for r in (await s.execute(
            __import__("sqlalchemy").select(CryptoSignalLog))).scalars()}
    assert got[ids[0]] == ("expired", 0.3) and got[ids[1]][0] == "won" and got[ids[2]][0] == "lost"
    backup = next((tmp_path / "backups").glob("crypto_signal_log_*.jsonl.gz"))
    assert len(gzip.open(backup, "rt").read().splitlines()) == 3
