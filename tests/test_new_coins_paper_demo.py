"""18 Oct 2026 owner: gold and TIA trade in paper and the demo; live only once added to 'Coins allowed on live'."""
from types import SimpleNamespace

import pytest

from config.settings import settings
from scheduler.runner import AppRunner


@pytest.mark.asyncio
async def test_gold_goes_to_the_demo_but_not_live(monkeypatch):
    monkeypatch.setattr(settings, "delta_india_mode", "live")
    monkeypatch.setattr(settings, "delta_testnet_mirror", True)
    r = AppRunner.__new__(AppRunner)
    real_one = AppRunner._delta_call_one
    reached = []

    async def one(self, target, method, *args, usdt_inr=83.0):
        reached.append(target)
        return SimpleNamespace(ok=True, reason="", delta_symbol="XAUTUSD", size=2, risk_inr=14.0, leverage=10)
    # the gate sits at the top of _delta_call_one: run it, then hand anything that passes to the stub
    async def gated(target, method, *args, usdt_inr=83.0):
        allowed = {x.strip().upper() for x in settings.delta_live_coins.split(",") if x.strip()}
        if target == "live" and method == "on_paper_open" and str(args[0]).upper() not in allowed:
            return await real_one(r, target, method, *args, usdt_inr=usdt_inr)   # must return None without I/O
        return await one(r, target, method, *args, usdt_inr=usdt_inr)
    r._delta_call_one = gated
    await r._mirror_delta_india("xautusdt", "long", 4163.0, 4080.0, 4413.0, "swing_keltner_break")
    assert reached == ["testnet"]
    reached.clear()
    await r._mirror_delta_india("xrpusdt", "short", 1.30, 1.38, 1.06, "swing_donchian")
    assert reached == ["live", "testnet"]


@pytest.mark.asyncio
async def test_live_gate_returns_none_without_touching_delta(monkeypatch):
    from pydantic import SecretStr
    monkeypatch.setattr(settings, "delta_india_api_key", SecretStr("k"))
    monkeypatch.setattr(settings, "delta_india_api_secret", SecretStr("s"))
    r = AppRunner.__new__(AppRunner)
    assert await r._delta_call_one("live", "on_paper_open", "tiausdt", "short", 0.66, 0.70, 0.54, "x") is None


def test_defaults_put_gold_and_tia_in_paper_but_not_live():
    from config.settings import Settings
    s = Settings()
    assert {"XAUTUSDT", "TIAUSDT"} <= set(s.swing_extra_symbols.split(","))
    live = set(s.delta_live_coins.split(","))
    assert "XAUTUSDT" not in live and "TIAUSDT" not in live and "XRPUSDT" in live and len(live) == 10


def test_gold_pays_gold_fees():
    from analysis.paper_cycle import fees_for
    assert fees_for("xautusdt").taker_pct < fees_for("tiausdt").taker_pct
