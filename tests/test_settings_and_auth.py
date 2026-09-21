import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from storage.database import Base
from storage.repository import Repository
from scheduler.runner import AppRunner
from scheduler.health import make_app


@pytest.fixture
async def mem_sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


@pytest.mark.asyncio
async def test_admin_auth_and_configs(mem_sessionmaker):
    async with mem_sessionmaker() as session:
        repo = Repository(session)

        # 1. Test Admin Auth
        pwd = "secret_test_password_123"
        await repo.set_admin_password(pwd)

        ok, token = await repo.verify_admin_password(pwd)
        assert ok is True
        assert token is not None and len(token) == 64

        bad_ok, bad_token = await repo.verify_admin_password("wrong_password")
        assert bad_ok is False
        assert bad_token is None

        valid = await repo.validate_session_token(token)
        assert valid is True

        invalid = await repo.validate_session_token("invalid_token")
        assert invalid is False

        await repo.invalidate_session_token(token)
        valid_after_logout = await repo.validate_session_token(token)
        assert valid_after_logout is False

        # 2. Test Paper Trading Config
        pcfg = await repo.get_paper_config()
        assert pcfg.starting_wallet == 3000.0
        assert pcfg.leverage == 10.0
        assert pcfg.enabled is True

        updated_pcfg = await repo.update_paper_config(starting_wallet=5000.0, leverage=15.0, enabled=False)
        assert updated_pcfg.starting_wallet == 5000.0
        assert updated_pcfg.leverage == 15.0
        assert updated_pcfg.enabled is False

        # 3. Test Strategy Config
        scfg = await repo.get_strategy_config()
        assert scfg.crypto_min_confidence == 0.70
        assert scfg.groq_model == "qwen/qwen3.8-27b"

        updated_scfg = await repo.update_strategy_config(
            crypto_min_confidence=0.75, groq_model="openai/gpt-oss-120b"
        )
        assert updated_scfg.crypto_min_confidence == 0.75
        assert updated_scfg.groq_model == "openai/gpt-oss-120b"


@pytest.mark.asyncio
async def test_settings_api_endpoints(mem_sessionmaker):
    runner = AppRunner()
    runner.notifier = AsyncMock()

    test_pwd = "my_custom_password_456"

    # Pre-seed password in memory DB
    async with mem_sessionmaker() as session:
        repo = Repository(session)
        await repo.set_admin_password(test_pwd)

    with patch("storage.database.AsyncSessionFactory", mem_sessionmaker):
        app = await make_app(runner)
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            # 1. Login with bad password -> 401
            resp = await client.post("/api/settings/auth/login", json={"password": "wrong"})
            assert resp.status == 401
            data = await resp.json()
            assert data["ok"] is False

            # 2. Login with correct password -> 200 + token
            resp = await client.post("/api/settings/auth/login", json={"password": test_pwd})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            token = data["token"]
            assert len(token) == 64

            # 3. Check auth status
            resp = await client.get("/api/settings/auth/status", headers={"X-Settings-Token": token})
            assert resp.status == 200
            assert (await resp.json())["authenticated"] is True

            # 4. GET & POST paper config
            resp = await client.get("/api/paper/config")
            assert resp.status == 200
            pdata = await resp.json()
            assert pdata["starting_wallet"] == 3000.0
            assert pdata["enabled"] is True

            # POST without auth -> 401
            resp = await client.post("/api/paper/config", json={"starting_wallet": 4500.0})
            assert resp.status == 401

            # POST with auth -> 200
            resp = await client.post(
                "/api/paper/config",
                headers={"X-Settings-Token": token},
                json={"starting_wallet": 4500.0, "leverage": 20.0, "enabled": False},
            )
            assert resp.status == 200
            assert (await resp.json())["starting_wallet"] == 4500.0
            assert (await resp.json())["enabled"] is False

            # 5. GET & POST strategy config
            resp = await client.get("/api/strategy/config")
            assert resp.status == 200
            sdata = await resp.json()
            assert sdata["groq_model"] == "qwen/qwen3.8-27b"

            resp = await client.post(
                "/api/strategy/config",
                headers={"X-Settings-Token": token},
                json={"crypto_min_confidence": 0.82},
            )
            assert resp.status == 200

            # 6. Toggle collector with token
            resp = await client.post(
                "/api/settings/toggle",
                headers={"X-Settings-Token": token},
                json={"collector": "coindcx", "enabled": False},
            )
            assert resp.status == 200
            assert runner.collector_enabled["coindcx"] is False

            # 7. Logout
            resp = await client.post("/api/settings/auth/logout", headers={"X-Settings-Token": token})
            assert resp.status == 200

            # 8. Post after logout -> 401
            resp = await client.post(
                "/api/paper/config",
                headers={"X-Settings-Token": token},
                json={"starting_wallet": 5000.0},
            )
            assert resp.status == 401

        finally:
            await client.close()
