"""18 Oct 2026: a real-money order (or a meaningful refusal) sends a Telegram message; routine skips do not."""
from types import SimpleNamespace

from scheduler.runner import live_order_alert


def _plan(**kw):
    base = dict(ok=True, reason="", delta_symbol="ADAUSD", side="sell", size=13, entry=0.2228, stop=0.2362,
                target=0.1826, risk_inr=15.2, inr_available=500.0, leverage=8)
    base.update(kw)
    return SimpleNamespace(**base)


def test_placed_order_says_coin_side_size_stop_and_risk():
    t = live_order_alert(_plan(), 0.03)
    assert "LIVE ₹ ORDER" in t and "ADA" in t and "SHORT" in t and "13 contracts" in t and "8x" in t
    assert "0.2362" in t and "₹15" in t and "3% rule" in t


def test_meaningful_refusals_alert_and_routine_skips_do_not():
    assert "NOT placed" in live_order_alert(_plan(ok=False, reason="daily_loss_limit"), 0.03)
    assert "NOT placed" in live_order_alert(_plan(ok=False, reason="leverage_not_set"), 0.03)
    assert "NOT placed" in live_order_alert(_plan(ok=False, reason="stop_rejected_closed"), 0.03)
    for quiet in ("below_one_contract", "delta_book_full", "already_open_on_delta", "kill_switch"):
        assert live_order_alert(_plan(ok=False, reason=quiet), 0.03) == ""
