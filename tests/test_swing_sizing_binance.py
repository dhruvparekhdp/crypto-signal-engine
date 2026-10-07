"""Swing sizing: one trade can no longer lock the whole wallet, and paper orders follow Binance USD-M rules.

7 Oct 2026: a 1x SOL trade took Rs1,238 of Rs1,735 free margin; every later signal was skipped and the book froze.
"""
import pytest

from analysis import binance_filters as bf
from analysis import swing_book as sb
from tests.test_swing_selection import run


def test_margin_cap_raises_leverage_instead_of_locking_the_wallet():
    # Rs3,000 wallet, 3% risk, 1.5% stop -> Rs6,000 notional. Old sizing: 2.2x on Rs2,700 (90% of the wallet).
    margin, lev = sb.size(3000, 3000, 0.03, 100.0, 98.5, 0.0, 10.0, max_margin_frac=0.15)
    assert margin <= 450 + 1e-9
    assert margin * lev == pytest.approx(4500)      # leverage ceiling 10x: risk trimmed, not the wallet locked
    old_margin, _ = sb.size(3000, 3000, 0.03, 100.0, 98.5, 0.0, 10.0)
    assert old_margin > 2000                         # the behaviour that froze the book (still available at 0)


def test_cap_does_not_change_a_trade_that_already_fits():
    a = sb.size(3000, 3000, 0.01, 100.0, 90.0, 0.0, 10.0, max_margin_frac=0.15)
    assert a[0] * a[1] == pytest.approx(300)         # risk 1% of 3000 / 10% stop = 300 notional
    assert a[0] <= 450


def test_binance_rules_round_down_and_enforce_min_order_value():
    assert bf.round_qty("SOLUSDT", 0.1199) == pytest.approx(0.11)
    assert bf.round_qty("DOGEUSDT", 57.9) == 57
    assert bf.check_order("SOLUSDT", 0.03, 120.0) == "below_min_notional"   # $3.6 < $5
    assert bf.check_order("SOLUSDT", 0.05, 120.0) is None                    # $6
    assert bf.check_order("BTCUSDT", 0.0005, 60000.0) == "below_one_lot"     # under 0.001 BTC
    assert bf.check_order("BTCUSDT", 0.001, 40000.0) == "below_min_notional" # $40 < $50
    assert bf.check_order("NEWCOINUSDT", 0.0001, 1.0) is None                # unknown: old rules apply


@pytest.mark.asyncio
async def test_second_and_third_signals_still_get_real_positions():
    rows, logs = await run([("SOLUSDT", "vol_breakout", +1), ("DOGEUSDT", "keltner_break", -1),
                            ("XRPUSDT", "ichimoku", +1)], cap=0, binance_rules=True)
    assert len(rows) == 3, [lg.skip_reason for lg in logs]
    wallet = 3000.0
    margins = sorted(r.margin for r in rows)
    assert margins[-1] <= wallet * 0.15 + 1          # no single trade locks the wallet
    assert margins[0] >= margins[-1] * 0.5           # the later trades are not crumbs


@pytest.mark.asyncio
async def test_signal_too_small_for_binance_is_skipped_with_reason(monkeypatch):
    monkeypatch.setattr("scheduler.runner.settings.swing_risk_pct", 0.0001)   # ~Rs0.3 risk -> far below $5
    rows, logs = await run([("SOLUSDT", "vol_breakout", +1)], cap=0, binance_rules=True)
    assert rows == []
    assert logs[0].skip_reason in {"below_min_notional", "below_one_lot"}


@pytest.mark.asyncio
async def test_btc_is_skipped_when_the_wallet_cannot_meet_binance_minimum():
    # Rs3,000 (~$29) at a 15% margin cap cannot reach the Binance BTC minimum (0.001 BTC, $50)
    rows, logs = await run([("BTCUSDT", "donchian", +1)], cap=0, binance_rules=True)
    assert rows == [] and logs[0].skip_reason in {"below_one_lot", "below_min_notional"}   # 0.0003 BTC < 0.001


def _trades(rows):
    import pandas as pd
    return pd.DataFrame([dict(symbol=s, side=1, entry_t=t, exit_t=t + 10**8, entry=px, exit=px, stop_frac=0.05,
                              net_ret=0.0, mae=0.0, reason="time", strategy="donchian", fee_frac=0.0)
                         for s, t, px in rows])


def test_lab_wallet_live_sizing_caps_margin_like_production():
    from analysis.lab.wallet import WalletConfig, run_wallet
    tr = _trades([("SOLUSDT", 1, 120.0), ("XRPUSDT", 2, 0.6), ("DOGEUSDT", 3, 0.2)])
    old = run_wallet(tr, WalletConfig(start=100, target=1e9, live_sizing=True, risk_pct=0.03, leverage=10,
                                      max_concurrent=99, reset=False))
    new = run_wallet(tr, WalletConfig(start=100, target=1e9, live_sizing=True, risk_pct=0.03, leverage=10,
                                      max_concurrent=99, reset=False, max_margin_frac=0.15))
    assert max(r["margin"] for r in old.ledger) > 50          # first trade takes most of the wallet
    assert max(r["margin"] for r in new.ledger) <= 15 + 1e-9  # capped at 15%
    assert len(new.ledger) == 3


def test_lab_wallet_binance_rules_skip_orders_binance_would_reject():
    from analysis.lab.wallet import WalletConfig, run_wallet
    tr = _trades([("BTCUSDT", 1, 60000.0)])                   # $29 wallet cannot buy 0.001 BTC
    res = run_wallet(tr, WalletConfig(start=29, target=1e9, live_sizing=True, risk_pct=0.03, leverage=10,
                                      max_concurrent=99, reset=False, max_margin_frac=0.15, binance_rules=True))
    assert res.ledger == [] and set(res.skipped) & {"below_one_lot", "below_min_notional"}
