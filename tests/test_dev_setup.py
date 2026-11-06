"""Dev setup: names-only env example, offline sockets, no exchange keys, lint gate."""
from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from scripts.dev_gate import excess
from tests.offline_guard import EXCHANGE_ENV_KEYS, OfflineNetworkError

ROOT = Path(__file__).resolve().parents[1]

_ENV_NAMES = (
    "PORT",
    "DATABASE_URL",
    "DATABASE_SSL_CA",
    "ADMIN_PASSWORD",
    "API_AUTH_TOKEN",
    "SENTIMENT_INGEST_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "TWELVEDATA_API_KEY",
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "DELTA_INDIA_API_KEY",
    "DELTA_INDIA_API_SECRET",
    "DELTA_INDIA_TESTNET_API_KEY",
    "DELTA_INDIA_TESTNET_API_SECRET",
)


def test_env_example_is_names_only():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    listed: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        assert line.startswith("#"), line
        assert "=" not in line
        name = line.lstrip("#").strip()
        if name and name.isidentifier() and name.isupper():
            listed.add(name)
    missing = [name for name in _ENV_NAMES if name not in listed]
    assert missing == []


def test_offline_guard_blocks_external_names_and_allows_loopback():
    with pytest.raises(OfflineNetworkError):
        socket.create_connection(("example.com", 443), timeout=1)
    with pytest.raises(OfflineNetworkError):
        socket.getaddrinfo("example.com", 443)
    sock = socket.socket()
    try:
        sock.connect(("127.0.0.1", 1))
    except OfflineNetworkError:
        raise
    except OSError:
        pass
    finally:
        sock.close()


def test_exchange_credentials_are_absent():
    from config.settings import settings

    assert settings.binance_api_key is None
    assert settings.binance_api_secret is None
    assert settings.binance_testnet_api_key is None
    assert settings.binance_testnet_api_secret is None
    assert settings.delta_india_api_key is None
    assert settings.delta_india_api_secret is None
    assert settings.delta_india_testnet_api_key is None
    assert settings.delta_india_testnet_api_secret is None
    assert settings.live_trading_mode == "off"
    assert settings.delta_india_live_orders is False
    assert settings.delta_india_mode == "off"
    for name in EXCHANGE_ENV_KEYS:
        assert name not in os.environ


def test_exchange_clients_stay_on_the_injected_fake():
    import asyncio

    from execution.binance_futures import BinanceFutures
    from execution.delta_india import DeltaIndia

    class _Response:
        status_code = 200
        text = ""

        def __init__(self, payload: dict):
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _FakeHTTP:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def request(self, method: str, url: str, **kwargs: object) -> _Response:
            self.urls.append(url)
            if "binance" in url:
                return _Response({"serverTime": 1_700_000_000_000})
            return _Response({"success": True, "result": []})

    async def _run() -> None:
        fake = _FakeHTTP()
        binance = BinanceFutures("not-a-key", "not-a-secret", client=fake)
        await binance.sync_time()
        delta = DeltaIndia("not-a-key", "not-a-secret", client=fake)
        body = await delta.ping()
        assert body["ok"] is True
        assert len(fake.urls) == 2
        assert all(url.startswith("https://") for url in fake.urls)

    asyncio.run(_run())


def test_lint_gate_rejects_growth_and_allows_a_drop():
    baseline = {("a.py", "E501"): 2}
    assert excess({("a.py", "E501"): 1}, baseline) == []
    grown = excess({("a.py", "E501"): 3, ("b.py", "F401"): 1}, baseline)
    assert any("a.py" in line and "E501" in line for line in grown)
    assert any("b.py" in line and "F401" in line for line in grown)


def test_makefile_and_ci_cover_the_ticket():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for target in ("setup:", "lint:", "typecheck:", "test:", "audit:", "secrets:"):
        assert target in makefile
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pull_request:" in workflow
    assert "push:" in workflow
    assert "gitleaks detect" in workflow
    assert "pip_audit" in workflow or "dev_gate.py audit" in workflow
    assert "pytest" in workflow
    for secret in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "DELTA_INDIA_API_KEY",
                   "DELTA_INDIA_API_SECRET"):
        assert secret not in workflow
    pin = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "cryptography>=43,<46" in pin
    ignored = (ROOT / "quality" / "pip-audit-ignore.txt").read_text(encoding="utf-8")
    assert "PYSEC-2026-2141" in ignored
    assert "GHSA-537c-gmf6-5ccf" in ignored
