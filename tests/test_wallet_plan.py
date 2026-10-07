"""Owner plan (7 Oct 2026): Rs5,000 paper wallet, withdraw Rs2,500 at Rs10,000; risk between 1% and 3%."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from analysis import swing_book as sb


@pytest.fixture
def risk_settings(monkeypatch):
    from config.settings import settings
    for k, v in dict(swing_risk_pct=0.03, swing_risk_min=0.01, swing_adaptive_risk=True,
                     swing_weak_specs="4h@ichimoku,8h@ichimoku", swing_weak_spec_factor=0.5).items():
        monkeypatch.setattr(settings, k, v)
    return settings


def test_risk_is_3pct_when_the_book_is_healthy(risk_settings):
    assert sb.risk_for("swing_vol_breakout", "4h", dd=0.0, streak=0) == pytest.approx(0.03)


def test_risk_falls_in_drawdowns_and_streaks_but_never_below_1pct(risk_settings):
    assert sb.risk_for("swing_donchian", "8h", dd=0.12, streak=0) == pytest.approx(0.0225)   # 75% in a 10% dd
    assert sb.risk_for("swing_donchian", "8h", dd=0.25, streak=4) == pytest.approx(0.01)     # 0.75% -> floor 1%


def test_weak_specs_trade_at_half_risk(risk_settings):
    assert sb.risk_for("swing_ichimoku", "4h", dd=0.0, streak=0) == pytest.approx(0.015)
    assert sb.risk_for("swing_ichimoku", "8h", dd=0.25, streak=0) == pytest.approx(0.01)      # floor


def test_fixed_risk_when_adaptive_is_off(risk_settings, monkeypatch):
    monkeypatch.setattr(risk_settings, "swing_adaptive_risk", False)
    assert sb.risk_for("swing_donchian", "4h", dd=0.4, streak=9) == pytest.approx(0.03)


def _repo():
    repo = SimpleNamespace(session=MagicMock(), update_cycle_wallet=AsyncMock())
    return repo


@pytest.mark.asyncio
async def test_profit_is_withdrawn_at_10k_and_trading_continues(monkeypatch):
    from scheduler.runner import AppRunner
    from config.settings import settings
    monkeypatch.setattr(settings, "paper_sweep_at", 10000.0)
    monkeypatch.setattr(settings, "paper_sweep_amount", 2500.0)
    runner = AppRunner()
    runner.notifier = AsyncMock()
    cstate = SimpleNamespace(wallet=7000.0, positions=[SimpleNamespace(margin=3200.0)])
    repo = _repo()
    w = await runner._maybe_sweep(repo, SimpleNamespace(id=1), cstate, 7000.0, SimpleNamespace(alert_telegram=True))
    assert w == 4500.0 and cstate.wallet == 4500.0
    row = repo.session.add.call_args[0][0]
    assert (row.amount, row.equity_before, row.wallet_after) == (2500.0, 10200.0, 4500.0)
    runner.notifier.send_text.assert_awaited()


@pytest.mark.asyncio
async def test_no_withdrawal_below_the_line_or_without_free_cash(monkeypatch):
    from scheduler.runner import AppRunner
    from config.settings import settings
    monkeypatch.setattr(settings, "paper_sweep_at", 10000.0)
    monkeypatch.setattr(settings, "paper_sweep_amount", 2500.0)
    runner = AppRunner()
    runner.notifier = AsyncMock()
    repo = _repo()
    below = SimpleNamespace(wallet=9000.0, positions=[])
    assert await runner._maybe_sweep(repo, SimpleNamespace(id=1), below, 9000.0, SimpleNamespace(alert_telegram=False)) == 9000.0
    locked = SimpleNamespace(wallet=1000.0, positions=[SimpleNamespace(margin=9500.0)])   # equity 10.5k, cash 1k
    assert await runner._maybe_sweep(repo, SimpleNamespace(id=1), locked, 1000.0, SimpleNamespace(alert_telegram=False)) == 1000.0
    repo.session.add.assert_not_called()
