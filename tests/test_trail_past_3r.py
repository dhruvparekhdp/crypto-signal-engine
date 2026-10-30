"""Trail past 3R instead of a fixed 3R target (owner 30 Oct 2026: "yes switch to trailing past 3R")."""
import pytest

from analysis import swing_book as sb


def test_no_target_is_far_away_and_never_below_zero():
    assert sb.open_ended_target(100.0, 1, 5.0) == pytest.approx(350.0)        # 50R up
    assert sb.open_ended_target(100.0, -1, 5.0) == pytest.approx(10.0)        # a short never targets <= 0


def test_the_stop_trails_1r_behind_the_best_price_only_past_3r():
    # short at 100, 1R = 5 (initial stop 105)
    assert sb.trail_past(-1, 100.0, 105.0, 98.0, 90.0, 3.0, 1.0) == (98.0, False)    # best +2R: ladder only
    stop, on = sb.trail_past(-1, 100.0, 105.0, 98.0, 85.0, 3.0, 1.0)                 # best +3R
    assert on and stop == pytest.approx(90.0)                                        # 1R behind 85
    stop, on = sb.trail_past(-1, 100.0, 105.0, 90.0, 80.0, 3.0, 1.0)                 # runs to +4R
    assert stop == pytest.approx(85.0)
    assert sb.trail_past(-1, 100.0, 105.0, 85.0, 82.0, 3.0, 1.0)[0] == 85.0          # never moves back
    assert sb.trail_past(1, 100.0, 95.0, 101.0, 120.0, 0.0, 1.0) == (101.0, False)   # 0 = off


def test_the_ladder_and_the_trail_agree_at_3r():
    # at +3R the ladder locks 1.75R (open-ended steps, 0.25R buffer); the trail locks 2R: the trail wins, never less
    lad, best = sb.swing_lock(1, 100.0, 95.0, 95.0, 0.0, 115.0, "ladder", 0, 0, "1:0,1.5:0.5,2:1,2.5:1.5", 0.25)
    assert lad == pytest.approx(108.75)
    assert sb.trail_past(1, 100.0, 95.0, lad, best, 3.0, 1.0)[0] == pytest.approx(110.0)


@pytest.mark.asyncio
async def test_live_trades_get_no_take_profit_and_the_trailing_stop_goes_on_delta_exactly(tmp_path):
    from tests.test_delta_ladder import FakeDelta, _book
    d = FakeDelta()
    book = _book(tmp_path, d, place_target=False)
    plan = await book.on_paper_open("xrpusdt", "short", 1.40, 1.47, 0.14, "swing_donchian", "swing_donchian@4h", 1)
    assert plan.ok and not any(k == "tp" for k, _ in d.calls)
    assert book._load_state()["positions"]["XRPUSD"]["tp_order_id"] is None
    res = await book.on_stop_change("xrpusdt", 1.26, "swing_donchian@4h", True)       # trailing: exactly there
    assert res["reason"] == "amended" and res["exchange_stop"] == pytest.approx(1.26)
    res = await book.on_stop_change("xrpusdt", 1.25, "swing_donchian@4h")            # ladder: 0.5R behind as before
    assert res["exchange_stop"] == pytest.approx(1.26)                                # 1.25 + 0.035 is looser: unchanged


@pytest.mark.asyncio
async def test_the_old_fixed_target_still_places_a_take_profit(tmp_path):
    from tests.test_delta_ladder import FakeDelta, _book
    d = FakeDelta()
    book = _book(tmp_path, d)
    assert (await book.on_paper_open("xrpusdt", "short", 1.40, 1.47, 1.19, "swing_donchian")).ok
    assert any(k == "tp" for k, _ in d.calls)


def test_the_setting_defaults_to_trailing():
    from config.settings import Settings
    f = Settings.model_fields
    assert f["swing_trail_after_r"].default == 3.0 and f["swing_trail_r"].default == 1.0


@pytest.mark.asyncio
async def test_a_new_paper_swing_trade_opens_with_the_normal_3r_target(monkeypatch):
    # owner 30 Oct 2026: "keep 3r normal swing target only" - trailing past 3R no longer removes the target
    from config.settings import settings
    from tests.test_phase1 import _swing
    monkeypatch.setattr(settings, "paper_mirrors_live", False)
    for trail in (3.0, 0.0):
        monkeypatch.setattr(settings, "swing_trail_after_r", trail)
        rows, _, _ = await _swing([("BTCUSDT", "donchian", 1, 60000.0)])
        risk = abs(rows[0].entry_price - rows[0].stop_price)
        assert (rows[0].target_price - rows[0].entry_price) / risk == pytest.approx(3, rel=0.01)
