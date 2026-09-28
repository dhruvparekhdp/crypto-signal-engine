"""
Repository.get_ai_vs_reality — the join behind /ai-vs-reality and
/api/ai-reality (28 Sep).

Part 2, "AI vs Reality": one row per Groq Sentinel pre-trade review, joined
read-only against what the market actually did (signal_audit's own
resolution, reused rather than recomputed) and what the paper trade did, if
one opened. No foreign key ties any of these three together — the join is
by (symbol, signal_type, proximity in time), the same pattern
reviewer_scorecard already established in this file, plus PaperTrade's own
signal_price + opened_at for the paper-trade side.

A fresh in-memory SQLite DB per test (the idiom in test_mirror_review.py and
test_ai_review_can_block_trade.py), not the shared on-disk DB
test_reviewer_scorecard.py uses — reviewer_scorecard aggregates every "pre"
review in the whole test run by verdict with no symbol scoping, so sharing
that DB here would silently change its numbers depending on test order.
"""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from storage.database import Base
from storage.repository import Repository


async def _fresh_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_review_with_no_matching_signal_is_still_returned():
    engine, sm = await _fresh_db()
    async with sm() as s:
        repo = Repository(s)
        await repo.save_review(
            "pre", "adausdt", signal_type="confluence", verdict="APPROVE",
            summary="clean setup", confidence_delta=0.0)
        rows = await repo.get_ai_vs_reality(days=7)
    assert len(rows) == 1
    assert rows[0]["ai"]["verdict"] == "APPROVE"
    assert rows[0]["market"] is None
    assert rows[0]["paper"]["status"] == "unmatched"
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_rejected_signal_never_opened_and_says_why():
    engine, sm = await _fresh_db()
    async with sm() as s:
        repo = Repository(s)
        await repo.log_crypto_signal(
            symbol="solusdt", signal_type="confluence", direction="long",
            trigger_description="t", confidence=0.66, current_price=100.0,
            target_price=101.0, stop_loss=99.0, edge_pct=1.0,
            stake_pct=0.01, timeframe="20m", rejection_reason="ai_review")
        await repo.save_review(
            "pre", "solusdt", signal_type="confluence", verdict="REJECT",
            summary="too thin", confidence_delta=-0.08)
        rows = await repo.get_ai_vs_reality(days=7)
    assert len(rows) == 1
    assert rows[0]["paper"]["status"] == "rejected"
    assert rows[0]["paper"]["reason"] == "ai_review"
    assert rows[0]["paper"]["opened"] is False
    # Ground truth still resolves independently of what the AI decided.
    assert rows[0]["market"] is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_won_signal_that_opened_and_won_shows_all_three_sides():
    engine, sm = await _fresh_db()
    async with sm() as s:
        repo = Repository(s)
        now = datetime.now(UTC).replace(tzinfo=None)
        sid = await repo.log_crypto_signal(
            symbol="bnbusdt", signal_type="confluence", direction="long",
            trigger_description="t", confidence=0.78, current_price=500.0,
            target_price=510.0, stop_loss=495.0, edge_pct=2.0,
            stake_pct=0.01, timeframe="20m")
        await repo.resolve_crypto_signal(sid, "won", 2.0)
        await repo.save_review(
            "pre", "bnbusdt", signal_type="confluence", verdict="APPROVE",
            summary="strong confluence", confidence_delta=0.02)
        # A closed paper trade quoted at the signal's own price and opened
        # close to the signal's own timestamp — the join key
        # Repository.get_ai_vs_reality actually uses (no FK exists).
        from storage.models import PaperTrade
        s.add(PaperTrade(
            cycle_id=1, symbol="bnbusdt", side="long",
            signal_price=500.0, entry_price=500.2, exit_price=510.0,
            margin=100.0, leverage=10.0, gross_pnl=20.0, net_pnl=18.0,
            return_on_margin=0.18, wallet_after=1018.0,
            exit_reason="target", hours_held=0.1,
            opened_at=now + timedelta(minutes=1),
            closed_at=now + timedelta(minutes=7)))
        await s.commit()
        rows = await repo.get_ai_vs_reality(days=7)
    assert len(rows) == 1
    r = rows[0]
    assert r["ai"]["verdict"] == "APPROVE"
    assert r["market"]["outcome"] == "won"
    assert r["paper"]["status"] == "closed"
    assert r["paper"]["opened"] is True
    assert r["paper"]["won"] is True
    assert r["paper"]["net_pnl"] == pytest.approx(18.0)
    await engine.dispose()


@pytest.mark.asyncio
async def test_shape_holds_on_a_fresh_install():
    """Called with nothing to grade, this must return an empty list rather
    than raise — the page asks for it unconditionally."""
    engine, sm = await _fresh_db()
    async with sm() as s:
        rows = await Repository(s).get_ai_vs_reality(days=7)
    assert rows == []
    await engine.dispose()
