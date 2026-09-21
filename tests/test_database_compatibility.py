from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analysis.crypto_state import CryptoState, OHLCVCandle
from analysis.crypto_state_store import CryptoStateStore
from scheduler.runner import AppRunner
from storage.database import Base
from storage.models import AdminAuth, CryptoSignalLog, _now_utc
from storage.repository import Repository


@pytest.fixture
async def memory_repo():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield Repository(session)
    await engine.dispose()


@pytest.mark.asyncio
async def test_now_utc_returns_naive_datetime():
    """Verify _now_utc() in models and repository returns naive UTC for DB compatibility."""
    now = _now_utc()
    assert now.tzinfo is None
    # Ensure it can be subtracted from naive UTC datetimes without TypeError
    diff = datetime.now(UTC).replace(tzinfo=None) - now
    assert abs(diff.total_seconds()) < 5


@pytest.mark.asyncio
async def test_repository_handles_datetime_operations_cleanly(memory_repo: Repository):
    """Test every timestamp-driven method in Repository works without tzinfo type errors."""
    repo = memory_repo

    # 1. Log crypto signal
    await repo.log_crypto_signal(
        symbol="solusdt",
        signal_type="confluence",
        direction="long",
        trigger_description="Confluence test",
        confidence=0.85,
        current_price=150.0,
        target_price=160.0,
        stop_loss=145.0,
        edge_pct=2.0,
        stake_pct=0.02,
        timeframe="15m",
    )

    # 2. Get recent crypto signals
    signals = await repo.get_recent_crypto_signals(hours=24)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.symbol == "solusdt"
    assert sig.timestamp.tzinfo is None  # stored as naive UTC

    # 3. Pending crypto signals & signals between
    pending = await repo.pending_crypto_signals(older_than_minutes=0)
    assert len(pending) == 1

    between = await repo.crypto_signals_between(newer_than_days=1)
    assert len(between) == 1

    # 4. Ingest news sentiment
    items = [
        {
            "external_id": "test_ext_1",
            "symbol": "solusdt",
            "headline": "Solana surges",
            "score": 0.8,
            "published_at": datetime.now(UTC).isoformat(),
        },
        {
            "external_id": "test_ext_2",
            "symbol": "solusdt",
            "headline": "Solana breaks resistance",
            "score": 0.9,
            "published_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        },
    ]
    accepted, dupes = await repo.ingest_news_sentiment(items)
    assert accepted == 2
    assert dupes == 0

    recent_news = await repo.recent_news_sentiment("solusdt", hours=6)
    assert len(recent_news) == 2

    # 5. Admin password verify & session update
    await repo.set_admin_password("mypassword")
    ok, token = await repo.verify_admin_password("mypassword")
    assert ok is True
    assert token is not None

    # Check updated_at is naive UTC
    auth = await repo.session.get(AdminAuth, 1)
    assert auth is not None
    assert auth.updated_at.tzinfo is None


@pytest.mark.asyncio
async def test_signal_outcome_resolution_with_mixed_naive_and_aware_timestamps():
    """Verify signal resolution succeeds whether timestamps are naive or aware."""
    runner = AppRunner()
    runner.notifier = AsyncMock()
    runner.crypto_store = CryptoStateStore()

    # Create dummy state with candles having both naive and timezone-aware timestamps
    state = CryptoState(symbol="btcusdt", base_asset="BTC")
    t0_aware = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    t0_naive = datetime(2026, 9, 16, 12, 0)

    # Add candles: 1 naive, 1 aware
    c1 = OHLCVCandle(60000.0, 60100.0, 59900.0, 60050.0, 10.0, t0_naive)
    c2 = OHLCVCandle(
        60050.0, 61500.0, 60000.0, 61200.0, 15.0, t0_aware + timedelta(minutes=5)
    )
    state.candles_1m.extend([c1, c2])
    runner.crypto_store._states["btcusdt"] = state

    # Test resolution logic with a signal having aware timestamp
    sig_aware = CryptoSignalLog(
        id=1,
        symbol="btcusdt",
        signal_type="confluence",
        direction="long",
        trigger_description="Test long",
        confidence=0.8,
        current_price=60000.0,
        target_price=61000.0,
        stop_loss=59000.0,
        edge_pct=1.5,
        stake_pct=0.02,
        timeframe="15m",
        timestamp=datetime(2026, 9, 16, 11, 59, tzinfo=UTC),
        outcome="pending",
    )

    # Test resolution logic with a signal having naive timestamp
    sig_naive = CryptoSignalLog(
        id=2,
        symbol="btcusdt",
        signal_type="confluence",
        direction="long",
        trigger_description="Test long",
        confidence=0.8,
        current_price=60000.0,
        target_price=61000.0,
        stop_loss=59000.0,
        edge_pct=1.5,
        stake_pct=0.02,
        timeframe="15m",
        timestamp=datetime(2026, 9, 16, 11, 59),
        outcome="pending",
    )

    mock_repo = AsyncMock()
    mock_pcfg = MagicMock()
    mock_pcfg.max_hold_minutes = 240
    mock_repo.get_paper_config.return_value = mock_pcfg
    mock_repo.pending_crypto_signals.return_value = [sig_aware, sig_naive]

    with patch("scheduler.runner.AsyncSessionFactory") as mock_maker:
        mock_maker.return_value.__aenter__.return_value = AsyncMock()
        with patch("scheduler.runner.Repository", return_value=mock_repo):
            await runner._resolve_signal_outcomes_job()

    # Both signals should have been successfully resolved as "won"
    assert mock_repo.resolve_crypto_signal.call_count == 2
    mock_repo.resolve_crypto_signal.assert_any_call(1, "won", pytest.approx(1.6667, abs=1e-3))
    mock_repo.resolve_crypto_signal.assert_any_call(2, "won", pytest.approx(1.6667, abs=1e-3))
