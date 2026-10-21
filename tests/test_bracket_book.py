"""Channel-style bracket, paper research (21 Oct 2026)."""
from datetime import datetime, timedelta

import pytest

from analysis.bracket_book import advance, open_trade, summary

T0 = datetime(2026, 10, 21, 12, 0, 30)


def bars(rows, start=T0.replace(second=0) + timedelta(minutes=1)):
    return [{"timestamp": start + timedelta(minutes=i), "open": r[0], "high": r[1], "low": r[2], "close": r[3]}
            for i, r in enumerate(rows)]


def trade(side=1):
    return open_trade(7, "ethusdt", side, "confluence", "primary", "intraday", T0, 100.0)


def test_first_target_moves_the_stop_to_entry_then_a_pullback_closes_at_entry():
    tr = advance(trade(), bars([(100, 100.2, 99.9, 100.1), (100.1, 101.1, 100.0, 101.0), (101, 102.8, 100.9, 102.7),
                                (102.7, 102.8, 99.9, 100.0)]))
    assert tr["status"] == "closed" and tr["exit"] == "stop at entry" and tr["legs"] == 2 and tr["tp1"]
    assert tr["net_pct"] == pytest.approx((0.0101 + 0.0276) / 4 * 100 - 0.15)
    assert tr["roi_5x_pct"] == pytest.approx(tr["net_pct"] * 5, abs=0.01)


def test_a_bar_touching_both_before_the_first_target_is_the_full_stop():
    tr = advance(trade(), bars([(100, 101.5, 94.9, 101.2)]))
    assert tr["exit"] == "stop" and tr["net_pct"] == pytest.approx(-5.15) and not tr["tp1"]


def test_shorts_mirror_longs_and_the_48h_limit_closes_the_rest():
    tr = advance(trade(-1), bars([(100, 100.1, 98.9, 99.0)] + [(99.0, 99.2, 98.8, 99.0)] * (48 * 60)))
    assert tr["legs"] == 1 and tr["exit"] == "48h limit"
    assert tr["net_pct"] == pytest.approx((0.0101 / 4 + 0.75 * 0.01) * 100 - 0.15, abs=1e-6)


def test_it_waits_for_bars_after_the_signal_and_resumes_where_it_stopped():
    tr = trade()
    assert advance(tr, [])["status"] == "waiting"
    tr = advance(tr, bars([(100, 100.5, 99.5, 100.2)]))
    assert tr["status"] == "open" and tr["entry"] == 100
    tr = advance(tr, bars([(100, 100.5, 99.5, 100.2), (100.2, 101.2, 100.1, 101.0)]))   # first bar not re-read
    assert tr["legs"] == 1 and tr["stop"] == 100


def test_summary_groups_by_source_role_and_coin():
    a = advance(trade(), bars([(100, 101.5, 99.9, 101.2)] * 2))
    b = advance(open_trade(8, "ethusdt", -1, "confluence", "mirror", "intraday", T0, 100.0), bars([(100, 105.5, 99.9, 105)]))
    s = summary([a, b])
    assert s["all"]["n"] == 2 and s["role:mirror"]["full_stops"] == 1 and s["role:primary"]["tp1_pct"] == 100.0


async def test_the_job_opens_a_trade_per_new_signal_and_closes_it_on_candles(tmp_path, monkeypatch):
    import json

    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from analysis.crypto_state import CryptoState, OHLCVCandle
    from scheduler.runner import AppRunner
    from storage.models import Base, CryptoSignalLog
    from storage.repository import Repository
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("scheduler.runner.AsyncSessionFactory", sm)
    r = AppRunner()
    r._BRACKET_OPEN, r._BRACKET_CLOSED = str(tmp_path / "book.json"), str(tmp_path / "closed.jsonl")
    log = dict(signal_type="confluence", trigger_description="x", confidence=0.6, current_price=100.0, target_price=101,
               stop_loss=99, edge_pct=0, stake_pct=0, timeframe="1h")
    async with sm() as s:
        await Repository(s).log_crypto_signal(symbol="ethusdt", direction="long", **log)
    now = datetime(2026, 10, 21, 12, 30)
    await r._bracket_paper_job(now)                               # first run: starts after the newest signal
    async with sm() as s:
        await Repository(s).log_crypto_signal(symbol="ethusdt", direction="long", **log)
        await s.execute(update(CryptoSignalLog).where(CryptoSignalLog.id == 2).values(timestamp=now))
        await s.commit()
    st = CryptoState(symbol="ethusdt", base_asset="ETH")
    st.candles_1m = [OHLCVCandle(100, 100.2, 99.9, 100.1, 1, now + timedelta(minutes=1)),
                     OHLCVCandle(100.1, 101.2, 95.0, 96.0, 1, now + timedelta(minutes=2))]   # touches both: stop
    r.crypto_store._states["ethusdt"] = st
    out = await r._bracket_paper_job(now + timedelta(minutes=5))
    assert out == {"new": 1, "open": 0, "closed": 1}
    closed = [json.loads(ln) for ln in open(tmp_path / "closed.jsonl")]
    assert closed[0]["exit"] == "stop" and closed[0]["role"] == "primary" and closed[0]["source"] == "intraday"
