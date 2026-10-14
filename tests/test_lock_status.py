"""14 Oct 2026, owner: show each open trade's trailing-profit (ladder) state on the portal."""
import pytest

from analysis.swing_book import ladder_status


def test_xrp_like_short_with_half_r_locked():
    # short entry 1.4198, original stop 1.4917 (1R = 0.0719), best 1.3114 (+1.51R), stop now 1.38378, mark 1.3349
    s = ladder_status(-1, 1.4198, 1.4917, 1.38378, 1.3114, 1.3349, 19.7, 102.0, buffer_r=0.25, confirm_min=60)
    assert s["state"].startswith("profit locked +0.5")
    assert s["r_now"] == pytest.approx(1.18, abs=0.01) and s["best_r"] == pytest.approx(1.51, abs=0.01)
    assert s["next_trigger_r"] == 2.0 and s["next_lock_r"] == pytest.approx(0.75)
    assert s["next_trigger_price"] == pytest.approx(1.4198 - 2 * 0.0719, abs=1e-4)
    assert s["worst_case_inr"] < 0                                # stopping out now still keeps a profit
    assert "60-minute candle close" in s["confirm"]


def test_fresh_trade_shows_original_stop_and_first_step():
    s = ladder_status(+1, 100, 94, 94, 0, 101, 1, 1)
    assert s["state"] == "original stop" and s["locked_r"] == -1.0
    assert s["next_trigger_r"] == 1.0 and s["next_lock_r"] == 0 and s["worst_case_inr"] == 6


def test_beyond_the_listed_steps_the_next_step_keeps_going():
    s = ladder_status(+1, 100, 94, 109, 117, 116, 1, 1)          # best 2.83R
    assert s["next_trigger_r"] == 3.0 and s["next_lock_r"] == 2.0
