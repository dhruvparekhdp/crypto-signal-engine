"""18 Oct 2026 owner: keep paper and the demo running alongside live. Every paper swing event goes to the live book
first and then to the demo; one never blocks the other; the kill switch stops only real money."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.settings import settings
from scheduler.runner import AppRunner


@pytest.mark.parametrize("mode,mirror,expected", [
    ("live", True, ["live", "testnet"]), ("live", False, ["live"]), ("testnet", False, ["testnet"]),
    ("testnet", True, ["testnet"]), ("shadow", True, ["shadow", "testnet"]), ("off", True, []),
])
def test_targets(monkeypatch, mode, mirror, expected):
    monkeypatch.setattr(settings, "delta_india_mode", mode)
    monkeypatch.setattr(settings, "delta_testnet_mirror", mirror)
    assert AppRunner._delta_targets() == expected


@pytest.mark.asyncio
async def test_open_goes_to_live_first_then_demo_and_a_live_failure_does_not_block_the_demo(monkeypatch):
    monkeypatch.setattr(settings, "delta_india_mode", "live")
    monkeypatch.setattr(settings, "delta_testnet_mirror", True)
    r = AppRunner.__new__(AppRunner)
    calls = []

    async def one(target, method, *args, usdt_inr=83.0):
        calls.append((target, method))
        return None if target == "live" else SimpleNamespace(ok=True, reason="", delta_symbol="ADAUSD", size=13,
                                                             risk_inr=15.0, leverage=8)
    r._delta_call_one = one
    await r._mirror_delta_india("adausdt", "short", 0.22, 0.233, 0.18, "swing_donchian")
    assert calls == [("live", "on_paper_open"), ("testnet", "on_paper_open")]
    calls.clear()
    await r._delta_call("on_stop_change", "adausdt", 0.22)
    assert calls == [("live", "on_stop_change"), ("testnet", "on_stop_change")]


@pytest.mark.asyncio
async def test_kill_switch_stops_live_but_not_the_demo(monkeypatch, tmp_path):
    from pydantic import SecretStr

    import execution.delta_book as db
    monkeypatch.setattr(settings, "delta_india_kill_switch", True)
    monkeypatch.setattr(settings, "delta_india_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "delta_india_api_secret", SecretStr("s"))
    monkeypatch.setattr(settings, "delta_india_testnet_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "delta_india_testnet_api_secret", SecretStr("s"))
    seen = {}
    real = db.DeltaBook.__init__

    def spy(self, client, **kw):
        seen[kw["state_path"].name] = kw["kill_switch"]
        real(self, client, **{**kw, "state_path": tmp_path / kw["state_path"].name,
                               "shadow_path": tmp_path / kw["shadow_path"].name})
    monkeypatch.setattr(db.DeltaBook, "__init__", spy)
    monkeypatch.setattr(db.DeltaBook, "reconcile", AsyncMock(return_value=[]))
    r = AppRunner.__new__(AppRunner)
    await r._delta_call_one("live", "reconcile")
    await r._delta_call_one("testnet", "reconcile")
    assert seen == {"delta_state.json": True, "delta_testnet_state.json": False}
