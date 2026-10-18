"""18 Oct 2026 owner: extra coins trade in paper and the demo; live only once on 'Coins allowed on live'. Gold and TIA
were paper-only first and joined the live list the same day ("try to add more coins"); DOT stands in for a paper-only
coin here."""
from types import SimpleNamespace

import pytest

from config.settings import settings
from scheduler.runner import AppRunner


@pytest.mark.asyncio
async def test_a_paper_only_coin_goes_to_the_demo_but_not_live(monkeypatch):
    monkeypatch.setattr(settings, "delta_india_mode", "live")
    monkeypatch.setattr(settings, "delta_testnet_mirror", True)
    r = AppRunner.__new__(AppRunner)
    real_one = AppRunner._delta_call_one
    reached = []

    async def one(self, target, method, *args, usdt_inr=83.0):
        reached.append(target)
        return SimpleNamespace(ok=True, reason="", delta_symbol="DOTUSD", size=2, risk_inr=14.0, leverage=10)
    # the gate sits at the top of _delta_call_one: run it, then hand anything that passes to the stub
    async def gated(target, method, *args, usdt_inr=83.0):
        allowed = {x.strip().upper() for x in settings.delta_live_coins.split(",") if x.strip()}
        if target == "live" and method == "on_paper_open" and str(args[0]).upper() not in allowed:
            return await real_one(r, target, method, *args, usdt_inr=usdt_inr)   # must return None without I/O
        return await one(r, target, method, *args, usdt_inr=usdt_inr)
    r._delta_call_one = gated
    await r._mirror_delta_india("dotusdt", "long", 4.1, 3.9, 4.7, "swing_keltner_break")
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
    assert await r._delta_call_one("live", "on_paper_open", "dotusdt", "short", 4.1, 4.3, 3.5, "x") is None


def test_defaults_put_gold_and_tia_in_paper_and_live():
    from config.settings import Settings
    s = Settings()
    assert {"XAUTUSDT", "TIAUSDT"} <= set(s.swing_extra_symbols.split(","))
    live = set(s.delta_live_coins.split(","))
    assert {"XAUTUSDT", "TIAUSDT", "XRPUSDT"} <= live and len(live) == 12


def test_gold_pays_gold_fees():
    from analysis.paper_cycle import fees_for
    assert fees_for("xautusdt").taker_pct < fees_for("tiausdt").taker_pct
