"""UI rebuild phase 0b: slim API payloads, ETag, signal pagination, portal polling."""
from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from scheduler.moves_api import run_detail_row, run_summary_row
from scheduler.signal_history_api import history_limit, project_signal_row
from storage.database import Base
from storage.models import CryptoSignalLog, MoveAttribution


class TestHistoryLimitDefault(unittest.TestCase):
    def test_legacy_default_is_four_hundred(self):
        from scheduler.signal_history_api import history_limit_for_request

        self.assertEqual(history_limit_for_request({}), 400)

    def test_paged_default_is_two_hundred(self):
        from scheduler.signal_history_api import history_limit_for_request

        self.assertEqual(history_limit_for_request({"cursor": ""}), 200)

    def test_legacy_high_limit_still_allowed(self):
        self.assertEqual(history_limit({"limit": "3000"}), 3000)


class TestSignalFieldsProjection(unittest.TestCase):
    def test_fields_drops_columns(self):
        row = {"id": 1, "symbol": "BTCUSDT", "direction": "long", "confidence": 80}
        slim = project_signal_row(row, "id,symbol,direction")
        self.assertEqual(slim, {"id": 1, "symbol": "BTCUSDT", "direction": "long"})


class TestMovesSummaryRows(unittest.TestCase):
    def test_summary_omits_heavy_bodies(self):
        row = MoveAttribution(
            id=7,
            window_hours=12,
            moves=json.dumps([{"symbol": "btcusdt", "change_4h": 1.2}]),
            signals=json.dumps([{"symbol": "btcusdt"}]),
            result=json.dumps({
                "overall": "BTC led. ETH followed.",
                "coins": [{"cause_type": "news"}, {"cause_type": "market_wide"}],
            }),
            model="test",
            latency_ms=1000,
            created_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC).replace(tzinfo=None),
        )
        summary = run_summary_row(row)
        self.assertNotIn("moves", summary)
        self.assertNotIn("signals", summary)
        self.assertEqual(summary["overall_preview"], "BTC led")
        self.assertEqual(summary["news_driven"], 1)
        detail = run_detail_row(row)
        self.assertIn("moves", detail)
        self.assertIn("result", detail)


class TestMovesApiPayloadSize(AioHTTPTestCase):
    async def get_application(self):
        from scheduler.health import make_app

        return await make_app(MagicMock())

    async def test_list_payload_under_eighty_kb(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        now = datetime.now(UTC).replace(tzinfo=None)
        fat_moves = [{"symbol": f"coin{i}usdt", "change_4h": i, "facts": {"x": "y" * 200}}
                     for i in range(40)]
        fat_coins = [{"symbol": f"coin{i}usdt", "cause_type": "news", "reasoning": "z" * 400,
                      "cause": "headline", "confidence": 0.8} for i in range(40)]
        async with Session() as session:
            for n in range(48):
                session.add(MoveAttribution(
                    window_hours=12,
                    moves=json.dumps(fat_moves),
                    signals=json.dumps([{"symbol": "btcusdt", "type": "t", "direction": "long"}]),
                    result=json.dumps({"overall": f"Run {n} " + ("word " * 30), "coins": fat_coins}),
                    model="m",
                    latency_ms=100,
                    created_at=now - timedelta(hours=n),
                ))
            await session.commit()

        with patch("storage.database.AsyncSessionFactory", Session):
            resp = await self.client.get("/api/moves?limit=48")
            body = await resp.read()
        self.assertLess(len(body), 80_000, f"list payload {len(body)} bytes")
        data = json.loads(body)
        self.assertEqual(len(data["runs"]), 48)
        self.assertNotIn("moves", data["runs"][0])
        await engine.dispose()


class TestMovesLatestInList(AioHTTPTestCase):
    async def get_application(self):
        from scheduler.health import make_app

        return await make_app(MagicMock())

    async def test_list_includes_latest_full_run(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as session:
            row = MoveAttribution(
                window_hours=4,
                moves=json.dumps([{"symbol": "btcusdt", "change_4h": 1.0}]),
                signals=json.dumps([]),
                result=json.dumps({"overall": "Newest run summary."}),
                model="m",
                latency_ms=1,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
            session.add(row)
            await session.commit()

        with patch("storage.database.AsyncSessionFactory", Session):
            resp = await self.client.get("/api/moves?limit=24")
            data = json.loads(await resp.text())
        self.assertIn("latest", data)
        self.assertIn("moves", data["latest"])
        self.assertNotIn("moves", data["runs"][0])
        await engine.dispose()


class TestMovesDetailEndpoint(AioHTTPTestCase):
    async def get_application(self):
        from scheduler.health import make_app

        return await make_app(MagicMock())

    async def test_detail_returns_full_run(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as session:
            row = MoveAttribution(
                window_hours=4,
                moves=json.dumps([{"symbol": "btcusdt"}]),
                signals=json.dumps([]),
                result=json.dumps({"overall": "ok"}),
                model="m",
                latency_ms=1,
                created_at=datetime.now(UTC).replace(tzinfo=None),
            )
            session.add(row)
            await session.commit()
            rid = row.id

        with patch("storage.database.AsyncSessionFactory", Session):
            resp = await self.client.get(f"/api/moves?detail={rid}")
            data = json.loads(await resp.text())
        self.assertIn("run", data)
        self.assertEqual(data["run"]["id"], rid)
        self.assertIsInstance(data["run"]["moves"], list)
        await engine.dispose()


class TestSignalHistoryPagination(AioHTTPTestCase):
    async def get_application(self):
        from scheduler.health import make_app

        return await make_app(MagicMock())

    async def test_cursor_envelope_and_legacy_array(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        now = datetime.now(UTC).replace(tzinfo=None)
        async with Session() as session:
            for i in range(6):
                session.add(CryptoSignalLog(
                    symbol="btcusdt",
                    signal_type="t",
                    direction="long",
                    trigger_description="test",
                    confidence=0.7,
                    current_price=1.0,
                    target_price=1.1,
                    stop_loss=0.9,
                    edge_pct=0.1,
                    stake_pct=1.0,
                    timeframe="1h",
                    outcome="pending",
                    pnl_pct=0.0,
                    timestamp=now - timedelta(minutes=i),
                ))
            await session.commit()

        with patch("storage.database.AsyncSessionFactory", Session):
            legacy = await self.client.get("/api/signals/history?days=1&limit=3")
            legacy_data = json.loads(await legacy.text())
            self.assertIsInstance(legacy_data, list)
            self.assertEqual(len(legacy_data), 3)

            page1 = await self.client.get("/api/signals/history?days=1&limit=2&cursor=")
            p1 = json.loads(await page1.text())
            self.assertIn("signals", p1)
            self.assertEqual(len(p1["signals"]), 2)
            self.assertIsNotNone(p1["next_cursor"])

            page2 = await self.client.get(f"/api/signals/history?days=1&limit=2&cursor={p1['next_cursor']}")
            p2 = json.loads(await page2.text())
            self.assertEqual(len(p2["signals"]), 2)
            ids1 = {s["id"] for s in p1["signals"]}
            ids2 = {s["id"] for s in p2["signals"]}
            self.assertFalse(ids1 & ids2)
        await engine.dispose()


class TestEtagMiddleware(AioHTTPTestCase):
    async def get_application(self):
        from scheduler.health import make_app

        runner = MagicMock()
        runner.crypto_store.get_symbols = AsyncMock(return_value=["btcusdt"])
        return await make_app(runner)

    async def test_if_none_match_returns_304(self):
        first = await self.client.get("/api/crypto/watchlist")
        self.assertEqual(first.status, 200)
        tag = first.headers.get("ETag")
        self.assertTrue(tag)
        second = await self.client.get("/api/crypto/watchlist", headers={"If-None-Match": tag})
        self.assertEqual(second.status, 304)
        self.assertEqual(await second.read(), b"")


class TestCoreJsPolling(unittest.TestCase):
    def test_coins_poll_fifteen_seconds(self):
        from pathlib import Path

        src = Path("scheduler/portal/core.js").read_text(encoding="utf-8")
        self.assertIn('Store.def("coins", "/api/crypto/coins", 15000)', src)
        self.assertIn("Store._stopTimer", src)
        self.assertIn('document.addEventListener("visibilitychange"', src)
