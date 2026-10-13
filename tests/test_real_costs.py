"""13 Oct 2026, owner: "charges are not calculated like trade fees, funding rate". Real funding + full open costs."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from analysis import funding as fmod

H8 = 8 * 3600
T0 = 1_791_849_600            # a settlement boundary (multiple of 8h)


def test_settlements_are_every_8h_after_opening():
    assert fmod.settlements(T0 + 60, T0 + 3 * H8 + 5) == [T0 + H8, T0 + 2 * H8, T0 + 3 * H8]
    assert fmod.settlements(T0 + 60, T0 + 3600) == []


def test_shorts_receive_positive_funding_and_pay_negative():
    rates = [(T0 + H8 - 3600, 0.01), (T0 + 2 * H8 - 3600, -0.05)]       # % per 8h
    # notional Rs10,000: +0.01% -> short receives Rs1; -0.05% -> short pays Rs5
    assert fmod.accrued(-1, 10_000, T0 + 60, T0 + 2 * H8 + 5, rates) == pytest.approx(-1.0 + 5.0)
    assert fmod.accrued(+1, 10_000, T0 + 60, T0 + 2 * H8 + 5, rates) == pytest.approx(1.0 - 5.0)


def test_unknown_rate_falls_back_to_a_conservative_cost():
    assert fmod.accrued(-1, 10_000, T0 + 60, T0 + H8 + 5, []) == pytest.approx(10_000 * 0.00655 / 100)


def test_close_uses_real_funding_when_set():
    from analysis.paper_cycle import fees_for
    from analysis.paper_trading import ExitReason, Side, close_position, open_position
    now = datetime(2026, 10, 13, tzinfo=UTC)
    pos = open_position(symbol="solusdt", side=Side.SHORT, entry_price=100.0, margin=1000.0, leverage=3.0,
                        fees=fees_for("solusdt"), stop_pct_of_margin=0.2, reward_risk=3.0, stop_price=105.0,
                        target_price=85.0, opened_at=now - timedelta(days=3), usdt_inr=1.0)
    flat = close_position(pos, 100.0, ExitReason.EXPIRY, now, fees_for("solusdt"), 1000.0)
    pos.funding_override = -4.0                                      # received Rs4
    real = close_position(pos, 100.0, ExitReason.EXPIRY, now, fees_for("solusdt"), 1000.0)
    assert real.funding_paid == pytest.approx(-4.0) and real.net_pnl > flat.net_pnl


@pytest.mark.asyncio
async def test_open_position_net_includes_entry_fee_exit_fee_and_funding(monkeypatch):
    from scheduler import health
    monkeypatch.setattr(health._SETTINGS, "paper_real_funding", True)
    opened = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
    row = SimpleNamespace(symbol="solusdt", side="short", coin_qty=1.0, entry_price=100.0, usdt_inr=100.0, margin=3000.0,
                          stop_price=105.0, target_price=85.0, liq_price=130.0, trail_active=False, confidence=0.8,
                          signal_type="swing_x", opened_at=opened, expires_at=None, trade_mode="swing", leverage=3.3,
                          entry_fee=5.9, tp1_price=0.0, tp2_price=0.0, partial_closed=False, partial_pnl=0.0)
    cycle = SimpleNamespace(id=2, wallet=2000.0, starting_wallet=5000, target_wallet=1e7, peak_wallet=5000, leverage=10,
                            status="running", started_at=opened, ended_at=None, note="")
    runner = SimpleNamespace(crypto_store=SimpleNamespace(get_all=AsyncMock(return_value=[])),
                             _perp_prices=AsyncMock(return_value={"SOLUSDT": 95.0}),
                             _real_funding=AsyncMock(return_value=-3.0))
    snap = {"cycle": cycle, "rows": [row], "trades": [], "running": [cycle], "recent": []}
    with patch.object(health, "_paper_db_snapshot", AsyncMock(return_value=snap)), \
         patch.object(health, "_paper_withdrawals", AsyncMock(return_value=[])), \
         patch.object(health, "_cycle_row", lambda c: {"id": c.id}), \
         patch("analysis.paper_cycle.config_for_cycle", return_value=SimpleNamespace()), \
         patch("analysis.paper_cycle.summarise", return_value={}):
        import json
        d = json.loads((await health._api_paper(runner, None)).text)
    p = d["positions"][0]
    gross = (100.0 - 95.0) * 1.0 * 100.0                                  # Rs500 on the perp mark
    assert p["mark"] == 95.0 and p["entry_fee"] == 5.9 and p["funding_so_far"] == -3.0
    assert p["unrealised"] == pytest.approx(gross - 5.9 - p["exit_fee_est"] + 3.0, abs=0.01)
    assert d["summary"]["open_entry_fees"] == 5.9 and d["summary"]["open_funding"] == -3.0
