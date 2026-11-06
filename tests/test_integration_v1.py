"""Read-only /integration/v1. In-memory database, no network, no orders."""
from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scheduler.integration_v1 import PATHS, bind_address, make_app
from storage.database import Base
from storage.models import (
    CryptoSignalLog,
    CryptoSnapshot,
    PaperCycle,
    PaperPosition,
    PaperTrade,
    PaperTradingConfig,
    StrategyRegistry,
)

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Correlation-ID": "cid-1"}
ROOT = Path(__file__).resolve().parents[1]
LOCAL_ROOTS = {"scheduler", "storage", "config"}


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    yield sessions
    await engine.dispose()


def _app(sessions, *, token=TOKEN, stale_after_s=3600):
    return make_app(sessions, token=token, stale_after_s=stale_after_s)


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed(sessions, when):
    async with sessions() as session:
        session.add(CryptoSnapshot(
            symbol="solusdt", price=150.25, volume_24h=10, rsi_14=55, timestamp=when,
        ))
        session.add(CryptoSignalLog(
            symbol="solusdt", signal_type="swing_donchian", direction="long",
            trigger_description="bar", confidence=0.8, current_price=150.25,
            target_price=160, stop_loss=144, edge_pct=0.1, stake_pct=0.02,
            timeframe="4h", timestamp=when, trade_mode="swing",
        ))
        session.add(CryptoSignalLog(
            symbol="btcusdt", signal_type="swing_keltner", direction="short",
            trigger_description="bar", confidence=0.7, current_price=60000,
            target_price=58000, stop_loss=61000, edge_pct=0.1, stake_pct=0.02,
            timeframe="4h", timestamp=when + timedelta(minutes=1), trade_mode="swing",
        ))
        session.add(PaperPosition(
            cycle_id=1, symbol="solusdt", side="long", signal_price=150,
            entry_price=150.25, margin=100, leverage=2, coin_qty=1.5, usdt_inr=85,
            stop_price=144, initial_stop_price=144, target_price=168, liq_price=100,
            entry_fee=0.1, opened_at=when, signal_type="swing_donchian",
            timeframe="4h", trade_mode="swing",
        ))
        session.add(PaperTrade(
            cycle_id=1, symbol="solusdt", side="long", trade_mode="swing",
            entry_price=150.25, exit_price=160, coin_qty=1.5, margin=100,
            leverage=2, gross_pnl=14, trading_fees=1.5, funding_paid=0,
            net_pnl=12.5, wallet_after=1012.5, exit_reason="target",
            signal_type="swing_donchian", timeframe="4h",
            opened_at=when, closed_at=when,
        ))
        session.add(PaperCycle(
            started_at=when, starting_wallet=1000, target_wallet=2000,
            wallet=1012.5, peak_wallet=1100, leverage=2, stop_pct_of_margin=0.2,
            reward_risk=3, min_confidence=0.7, status="running",
        ))
        session.add(PaperTradingConfig(id=1, max_concurrent=3))
        session.add(StrategyRegistry(
            spec_key="4h@vol_breakout", params="z=3.0", status="incubating",
            updated_at=when,
        ))
        await session.commit()


def _no_floats(value):
    if type(value) is float:
        raise AssertionError(value)
    if isinstance(value, dict):
        for item in value.values():
            _no_floats(item)
    elif isinstance(value, list):
        for item in value:
            _no_floats(item)


def _envelope(body):
    assert body["source_engine"] == "crypto-signal-engine"
    assert body["schema_version"] == "1.0"
    assert body["generated_at"].endswith("Z")
    assert body["data_as_of"].endswith("Z")
    assert body["status"] in {"ok", "degraded", "unavailable"}
    assert isinstance(body["is_stale"], bool)
    assert body["mode"] == "paper"
    assert isinstance(body["errors"], list)
    _no_floats(body)


@pytest.mark.asyncio
async def test_every_endpoint_reads_the_paper_book(db):
    when = _now()
    await _seed(db, when)
    async with TestClient(TestServer(_app(db))) as client:
        for path in PATHS:
            resp = await client.get(path, headers=AUTH)
            body = await resp.json()
            assert resp.status == 200, (path, body)
            _envelope(body)
            assert body["correlation_id"] == "cid-1"
            assert body["status"] == "ok"
            assert body["is_stale"] is False
        overview = await (await client.get("/integration/v1/market/overview", headers=AUTH)).json()
        quote = overview["data"]["quotes"][0]
        assert quote["symbol"] == "solusdt"
        assert quote["price"] == "150.25000000"
        assert quote["currency"] == "USDT"
        signals = await (await client.get("/integration/v1/signals", headers=AUTH)).json()
        assert [item["symbol"] for item in signals["data"]["items"]] == ["solusdt", "btcusdt"]
        assert signals["data"]["next_cursor"] is None
        assert signals["data"]["items"][0]["price"] == "150.25000000"
        positions = await (await client.get("/integration/v1/positions", headers=AUTH)).json()
        pos = positions["data"]["items"][0]
        assert pos["quantity"] == "1.50000000"
        assert pos["margin"] == {"amount": "100.00", "currency": "INR"}
        trades = await (await client.get("/integration/v1/trades", headers=AUTH)).json()
        trade = trades["data"]["items"][0]
        assert trade["net_pnl"] == {"amount": "12.50", "currency": "INR"}
        assert trade["exit_price"] == "160.00000000"
        perf = await (await client.get("/integration/v1/performance", headers=AUTH)).json()
        assert perf["data"]["closed_trades"] == 1
        assert perf["data"]["wins"] == 1
        assert perf["data"]["losses"] == 0
        assert perf["data"]["net_pnl"]["currency"] == "INR"
        assert perf["data"]["win_rate"] == "1.00000000"
        risk = await (await client.get("/integration/v1/risk", headers=AUTH)).json()
        assert risk["data"]["open_positions"] == 1
        assert risk["data"]["margin_in_use"]["currency"] == "INR"
        assert risk["data"]["open_notional"]["currency"] == "USDT"
        assert risk["data"]["max_concurrent"] == 3
        assert risk["data"]["cycle_status"] == "running"
        strategies = await (await client.get("/integration/v1/strategies", headers=AUTH)).json()
        assert strategies["data"]["items"][0]["spec_key"] == "4h@vol_breakout"
        assert strategies["data"]["items"][0]["source"] == "registry"


@pytest.mark.asyncio
async def test_list_limit_and_since(db):
    await _seed(db, _now())
    async with TestClient(TestServer(_app(db))) as client:
        first = await client.get("/integration/v1/signals?limit=1", headers=AUTH)
        body = await first.json()
        assert first.status == 200
        assert len(body["data"]["items"]) == 1
        cursor = body["data"]["next_cursor"]
        assert cursor
        second = await client.get(
            "/integration/v1/signals", params={"since": cursor, "limit": "1"}, headers=AUTH,
        )
        nxt = await second.json()
        assert nxt["data"]["items"][0]["symbol"] == "btcusdt"
        assert nxt["data"]["next_cursor"] is None
        too_big = await client.get("/integration/v1/trades?limit=501", headers=AUTH)
        assert too_big.status == 400
        assert (await too_big.json())["errors"][0]["code"] == "limit_too_large"
        bad = await client.get("/integration/v1/positions?since=yesterday", headers=AUTH)
        assert bad.status == 400
        assert (await bad.json())["errors"][0]["code"] == "bad_since"


@pytest.mark.asyncio
async def test_missing_or_wrong_token_is_401(db):
    async with TestClient(TestServer(_app(db))) as client:
        for path in PATHS:
            missing = await client.get(path)
            wrong = await client.get(path, headers={"Authorization": "Bearer no"})
            assert missing.status == 401
            assert wrong.status == 401
            body = await missing.json()
            _envelope(body)
            assert body["errors"][0]["code"] == "unauthorized"
    async with TestClient(TestServer(_app(db, token=""))) as client:
        resp = await client.get("/integration/v1/health", headers=AUTH)
        assert resp.status == 401


@pytest.mark.asyncio
async def test_mutations_are_405(db):
    async with TestClient(TestServer(_app(db))) as client:
        for path in PATHS:
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                resp = await client.request(method, path, headers=AUTH)
                assert resp.status == 405, (method, path)
                body = await resp.json()
                _envelope(body)
                assert body["errors"][0]["code"] == "method_not_allowed"
        other = await client.post("/integration/v1/orders", headers=AUTH)
        assert other.status == 405
        missing = await client.get("/integration/v1/orders", headers=AUTH)
        assert missing.status == 404


@pytest.mark.asyncio
async def test_stale_flag_follows_the_threshold(db):
    old = datetime(2020, 1, 1)
    async with db() as session:
        session.add(CryptoSnapshot(symbol="solusdt", price=1, timestamp=old))
        await session.commit()
    async with TestClient(TestServer(_app(db, stale_after_s=60))) as client:
        resp = await client.get("/integration/v1/market/overview", headers=AUTH)
        body = await resp.json()
        assert resp.status == 200
        assert body["is_stale"] is True
        assert body["status"] == "degraded"
        assert body["errors"][0]["code"] == "stale"
        assert body["data_as_of"] == "2020-01-01T00:00:00Z"
    async with TestClient(TestServer(_app(db, stale_after_s=3600))) as fresh:
        health = await (await fresh.get("/integration/v1/health", headers=AUTH)).json()
        assert health["is_stale"] is True
        assert health["status"] == "degraded"
        assert health["errors"][0]["code"] == "stale"


@pytest.mark.asyncio
async def test_empty_book_is_stale_and_strategies_come_from_settings(db):
    async with TestClient(TestServer(_app(db, stale_after_s=60))) as client:
        health = await (await client.get("/integration/v1/health", headers=AUTH)).json()
        assert health["is_stale"] is True
        assert health["errors"][0]["code"] == "no_data"
        assert health["data"]["database"] == "ok"
        strategies = await (await client.get("/integration/v1/strategies", headers=AUTH)).json()
        keys = [item["spec_key"] for item in strategies["data"]["items"]]
        assert "4h@vol_breakout" in keys
        assert strategies["data"]["items"][0]["source"] == "settings"
        assert strategies["is_stale"] is False


def test_default_bind_is_loopback(monkeypatch):
    monkeypatch.delenv("INTEGRATION_BIND", raising=False)
    monkeypatch.delenv("INTEGRATION_PORT", raising=False)
    assert bind_address() == ("127.0.0.1", 8091)
    monkeypatch.setenv("INTEGRATION_BIND", "127.0.0.1")
    monkeypatch.setenv("INTEGRATION_PORT", "9001")
    assert bind_address() == ("127.0.0.1", 9001)


@pytest.mark.asyncio
async def test_process_binds_loopback_only(db):
    app = _app(db)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sock = site._server.sockets[0]
    host, port = sock.getsockname()[:2]
    try:
        assert host == "127.0.0.1"
        async with ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{port}/integration/v1/health", headers=AUTH,
            ) as resp:
                assert resp.status == 200
                body = await resp.json()
                assert body["mode"] == "paper"
    finally:
        await runner.cleanup()


def _project_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def test_integration_module_does_not_import_execution():
    seen = set()
    stack = ["scheduler.integration_v1"]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        rel = Path(*name.split("."))
        path = ROOT / f"{rel}.py"
        if not path.exists():
            path = ROOT / rel / "__init__.py"
        if not path.exists():
            continue
        for imported in _project_imports(path):
            top = imported.split(".", 1)[0]
            assert top != "execution", f"{path} imports {imported}"
            if top in LOCAL_ROOTS:
                stack.append(imported)
    assert "scheduler.integration_v1" in seen
