"""
`bank_size` on the classic strategy-config page (/settings/classic ->
/api/strategy/config) saved into StrategyConfig.bank_size only. The live
`settings` object's own `bank_size` - the one notifications/crypto_formatter.py
actually reads to compute a Telegram alert's stake amount - was never
updated: `bank_size` was missing both from config/overrides.py's FIELDS (so
`apply()` silently dropped it - "field is None: continue") and from
LEGACY_STRATEGY (so the classic page's write-through never even tried).

The page said "saved", the number changed on screen, and nothing about the
real stake amount shown to the owner in a Telegram alert ever moved - the
same "looks live, silently isn't" shape as the paper-cycle config bug.

`min_confidence` (a second, unrelated field on the same StrategyConfig row
and the same classic page) is deliberately left alone: it has zero read
sites anywhere in the trading engine, superseded by crypto_min_confidence
and paper_min_confidence, and wiring it to actually do something would be a
new design decision, not a bug fix - see the owner's report for this.
"""
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config.settings import settings
from scheduler.health import make_app
from scheduler.runner import AppRunner
from storage.database import Base
from storage.repository import Repository


@pytest.fixture
async def mem_sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


@pytest.mark.asyncio
async def test_saving_bank_size_on_the_classic_page_updates_the_live_setting(mem_sessionmaker):
    original = settings.bank_size
    try:
        runner = AppRunner()
        runner.notifier = AsyncMock()
        test_pwd = "bank_size_test_pw"

        async with mem_sessionmaker() as session:
            await Repository(session).set_admin_password(test_pwd)

        with patch("storage.database.AsyncSessionFactory", mem_sessionmaker):
            app = await make_app(runner)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post("/api/settings/auth/login", json={"password": test_pwd})
                token = (await resp.json())["token"]

                assert settings.bank_size == 10000.0  # the code default, untouched so far

                resp = await client.post(
                    "/api/strategy/config",
                    headers={"X-Settings-Token": token},
                    json={"bank_size": 25000.0},
                )
                assert resp.status == 200

                # The row itself is updated either way - that part never was
                # the bug. The bug is that settings.bank_size (what the code
                # actually reads) used to stay frozen at the old value.
                async with mem_sessionmaker() as session:
                    cfg = await Repository(session).get_strategy_config()
                assert cfg.bank_size == 25000.0

                assert settings.bank_size == 25000.0
            finally:
                await client.close()
    finally:
        settings.bank_size = original


@pytest.mark.asyncio
async def test_bank_size_also_reaches_the_stake_amount_shown_in_a_telegram_alert(mem_sessionmaker):
    """End-to-end through the actual read site, not just the setting itself."""
    from datetime import UTC, datetime

    from analysis.crypto_signal import CryptoSignal
    from notifications.crypto_formatter import format_crypto_signal

    original = settings.bank_size
    try:
        settings.bank_size = 50000.0
        sig = CryptoSignal(
            symbol="btcusdt", signal_type="confluence", direction="long",
            trigger_description="test", confidence=0.8,
            current_price=60000.0, target_price=61200.0, stop_loss=59400.0,
            edge_pct=2.0, stake_pct=0.02, timeframe="15m", sentiment_score=0.0,
            indicators_summary="", timestamp=datetime.now(UTC))
        text = format_crypto_signal(sig)
        assert "1,000" in text or "1000" in text  # 0.02 * 50000 = 1000
    finally:
        settings.bank_size = original
