"""/keys: keys stored encrypted, never sent back to the page, editable from a phone, importable from .env."""
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from config import secret_box


@pytest.fixture
def master_key(monkeypatch):
    monkeypatch.setenv("SECRETS_MASTER_KEY", secret_box.new_master_key())


def test_seal_and_open_round_trip(master_key):
    sealed = secret_box.seal("gsk_live_1234567890")
    assert secret_box.is_sealed(sealed) and "gsk_live" not in json.dumps(sealed)
    assert secret_box.open_(sealed) == "gsk_live_1234567890"
    assert secret_box.open_("plain-old-row") == "plain-old-row"           # rows saved before encryption still load


def test_without_a_master_key_nothing_breaks(monkeypatch):
    monkeypatch.delenv("SECRETS_MASTER_KEY", raising=False)
    assert secret_box.seal("abc") == "abc" and not secret_box.available()


def test_a_wrong_master_key_cannot_open(master_key, monkeypatch):
    sealed = secret_box.seal("secret")
    monkeypatch.setenv("SECRETS_MASTER_KEY", secret_box.new_master_key())
    assert secret_box.open_(sealed) is None


def test_apply_decrypts_and_keeps_new_secret_fields_as_secretstr(master_key):
    from pydantic import SecretStr

    from config.overrides import apply, seal_secrets
    class Stub:
        model_fields = {"hf_space_api_key": SimpleNamespace(annotation=SecretStr | None),
                        "groq_api_key": SimpleNamespace(annotation=SecretStr | None)}
    s = Stub()
    s.groq_api_key = s.hf_space_api_key = None
    stored = seal_secrets({"groq_api_key": "gsk_abc12345", "hf_space_api_key": "space-key-9999"})
    assert all(secret_box.is_sealed(v) for v in stored.values())
    apply(s, stored)
    assert s.groq_api_key.get_secret_value() == "gsk_abc12345"
    assert isinstance(s.hf_space_api_key, SecretStr)            # was a plain str before, and crashed the Space call


@pytest.fixture
def db():
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mktemp(suffix='.db')}"
    yield


@pytest.mark.asyncio
async def test_keys_page_saves_encrypted_never_returns_keys_and_imports_env(master_key, db, monkeypatch):
    from config.settings import settings
    from scheduler import keys_page, settings_page
    from storage.database import AsyncSessionFactory, init_db
    from storage.repository import Repository

    await init_db()
    app = web.Application()
    keys_page.register(app, SimpleNamespace(on_settings_changed=lambda keys: None))
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    monkeypatch.setattr(settings, "hf_base_url", "")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:telegram-token-abcd")
    async with TestClient(TestServer(app)) as client:
        with patch.object(settings_page, "_admin", AsyncMock(return_value=web.json_response({}, status=401))):
            assert (await client.get("/api/keys")).status == 401                # admin only
        with patch.object(settings_page, "_admin", AsyncMock(return_value=None)):
            r = await client.post("/api/keys/save", json={"key": "openrouter_api_key", "value": "sk-or-v1-secretvalue7777"})
            assert (await r.json())["ok"]
            assert settings.openrouter_api_key.get_secret_value() == "sk-or-v1-secretvalue7777"   # applied now
            async with AsyncSessionFactory() as s:
                raw = (await Repository(s).get_app_settings())["openrouter_api_key"]
            assert secret_box.is_sealed(raw) and "secretvalue" not in json.dumps(raw)            # encrypted at rest

            body = await (await client.get("/api/keys")).json()
            assert body["encrypted_storage"] is True
            assert "secretvalue" not in json.dumps(body)                                        # never sent back
            row = {k["key"]: k for k in body["keys"]}["openrouter_api_key"]
            assert row["where"] == "database (encrypted)" and row["hint"] == "…7777"
            assert {k["key"]: k for k in body["keys"]}["telegram_bot_token"]["where"] == ".env only"

            moved = await (await client.post("/api/keys/import-env")).json()
            assert "telegram_bot_token" in moved["moved"]
            async with AsyncSessionFactory() as s:
                assert secret_box.open_((await Repository(s).get_app_settings())["telegram_bot_token"]) == \
                    "123456:telegram-token-abcd"

            monkeypatch.setattr(settings, "telegram_bot_token", settings.telegram_bot_token)
            await client.post("/api/keys/save", json={"key": "hf_base_url", "value": "https://dhruvdp-dhruv-llm.hf.space"})
            assert settings.hf_base_url == "https://dhruvdp-dhruv-llm.hf.space"

            assert (await (await client.post("/api/keys/clear", json={"key": "openrouter_api_key"})).json())["ok"]
            assert not settings.openrouter_api_key or settings.openrouter_api_key.get_secret_value() != "sk-or-v1-secretvalue7777"
            assert (await client.post("/api/keys/save", json={"key": "admin_password", "value": "x"})).status == 400


@pytest.mark.asyncio
async def test_plain_text_keys_saved_before_encryption_are_encrypted_at_start_up(master_key, db, monkeypatch):
    from scheduler import runner as runner_mod
    from storage.database import AsyncSessionFactory, init_db
    from storage.repository import Repository

    await init_db()
    async with AsyncSessionFactory() as s:
        await Repository(s).save_app_settings({"groq_api_key": "gsk_plain_old_row_1234", "session_end_utc": 18})
    monkeypatch.setattr(runner_mod, "AsyncSessionFactory", AsyncSessionFactory)
    from config.overrides import FIELDS
    from config.settings import settings as live
    for f in FIELDS:                                          # loading overrides touches every setting: restore all
        monkeypatch.setattr(live, f.key, getattr(live, f.key, None))
    r = runner_mod.AppRunner()
    await r.load_settings_overrides()
    async with AsyncSessionFactory() as s:
        stored = await Repository(s).get_app_settings()
    assert secret_box.is_sealed(stored["groq_api_key"]) and secret_box.open_(stored["groq_api_key"]) == "gsk_plain_old_row_1234"
    assert stored["session_end_utc"] == 18                                      # non-secret settings untouched
    from config.settings import settings
    assert settings.groq_api_key.get_secret_value() == "gsk_plain_old_row_1234"


@pytest.mark.asyncio
async def test_reveal_returns_the_value_only_to_the_admin(master_key, monkeypatch):
    from pydantic import SecretStr

    from config.settings import settings
    from scheduler import keys_page, settings_page
    app = web.Application()
    keys_page.register(app, SimpleNamespace())
    monkeypatch.setattr(settings, "groq_api_key", SecretStr("gsk_reveal_me_1234"))
    async with TestClient(TestServer(app)) as client:
        with patch.object(settings_page, "_admin", AsyncMock(return_value=web.json_response({}, status=401))):
            assert (await client.post("/api/keys/reveal", json={"key": "groq_api_key"})).status == 401
        with patch.object(settings_page, "_admin", AsyncMock(return_value=None)):
            r = await client.post("/api/keys/reveal", json={"key": "groq_api_key"})
            assert (await r.json())["value"] == "gsk_reveal_me_1234" and r.headers["Cache-Control"] == "no-store"
            assert (await client.post("/api/keys/reveal", json={"key": "database_url"})).status == 400


@pytest.mark.asyncio
async def test_admin_password_follows_env_when_it_changes(db, monkeypatch):
    from storage.database import AsyncSessionFactory, init_db
    from storage.repository import Repository
    monkeypatch.setenv("ADMIN_PASSWORD", "first-pass")
    await init_db()
    monkeypatch.setenv("ADMIN_PASSWORD", "second-pass")               # owner changes .env, restarts
    await init_db()
    async with AsyncSessionFactory() as s:
        assert (await Repository(s).verify_admin_password("second-pass"))[0]
    async with AsyncSessionFactory() as s:
        assert not (await Repository(s).verify_admin_password("first-pass"))[0]
