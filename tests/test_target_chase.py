"""The target always rests on Delta: normal 3R, moving out from 2.5R (owner 30 Oct 2026: "keep 3r normal swing target
only, increase when it reaches 2.5R +"). Fake exchange: no network, no real orders."""
import pytest

from analysis import swing_book as sb
from tests.test_delta_ladder import FakeDelta, _book


def test_the_target_is_3r_until_2_5r_then_stays_half_an_r_beyond_the_best_price():
    # long 100, stop 95 (1R = 5): 3R = 115
    assert sb.chase_target(1, 100.0, 95.0, 115.0, 110.0, 2.5, 0.5) == 115.0         # 2R: unchanged
    assert sb.chase_target(1, 100.0, 95.0, 115.0, 112.5, 2.5, 0.5) == 115.0         # 2.5R: 112.5 + 2.5 = 115
    assert sb.chase_target(1, 100.0, 95.0, 115.0, 114.0, 2.5, 0.5) == 116.5         # 2.8R: moves out
    assert sb.chase_target(1, 100.0, 95.0, 116.5, 113.0, 2.5, 0.5) == 116.5         # never back
    # short 1.40, stop 1.47 (1R = 0.07): 3R = 1.19
    assert sb.chase_target(-1, 1.40, 1.47, 1.19, 1.20, 2.5, 0.5) == pytest.approx(1.165)


def test_a_trade_opened_with_no_target_gets_the_normal_3r_one():
    # gold short opened 30 Oct while trades had a 50R "no target": 3969.18, stop 4043.42
    t = sb.chase_target(-1, 3969.18, 4043.42, 396.96, 3960.0, 2.5, 0.5)
    assert t == pytest.approx(3969.18 - 3 * 74.24)


def test_the_settings_default_to_moving_from_2_5r_half_an_r_ahead():
    from config.settings import Settings
    f = Settings.model_fields
    assert f["swing_target_chase_after_r"].default == 2.5 and f["swing_target_chase_gap_r"].default == 0.5


async def _open(book, place_target=True):
    return await book.on_paper_open("xrpusdt", "short", 1.40, 1.47, 1.19, "swing_donchian", "swing_donchian@4h", 1)


@pytest.mark.asyncio
async def test_new_live_trades_place_the_3r_take_profit(tmp_path):
    d = FakeDelta()
    book = _book(tmp_path, d)
    assert (await _open(book)).ok
    tp = [kw for k, kw in d.calls if k == "tp"]
    assert tp and tp[0]["stop_price"] == pytest.approx(1.19)
    assert book._load_state()["positions"]["XRPUSD"]["exchange_target"] == pytest.approx(1.19)


@pytest.mark.asyncio
async def test_the_take_profit_moves_out_on_delta_and_never_back(tmp_path):
    d = FakeDelta()
    book = _book(tmp_path, d)
    await _open(book)
    tp_id = book._load_state()["positions"]["XRPUSD"]["tp_order_id"]
    res = await book.on_target_change("xrpusdt", 1.165, "swing_donchian@4h")
    assert res["reason"] == "amend_target" and d.calls[-1] == ("edit", {"id": tp_id, "stop_price": pytest.approx(1.165)})
    n = len(d.calls)
    assert (await book.on_target_change("xrpusdt", 1.18, "swing_donchian@4h"))["reason"] == "unchanged"
    assert len(d.calls) == n


@pytest.mark.asyncio
async def test_a_trade_without_a_take_profit_gets_one(tmp_path):
    d = FakeDelta()
    book = _book(tmp_path, d, place_target=False)                 # like the gold short opened 30 Oct
    await _open(book)
    assert not any(k == "tp" for k, _ in d.calls)
    res = await book.on_target_change("xrpusdt", 1.19, "swing_donchian@4h")
    assert res["reason"] == "place_target" and d.calls[-1][0] == "tp"
    assert book._load_state()["positions"]["XRPUSD"]["tp_order_id"] is not None


@pytest.mark.asyncio
async def test_an_older_trade_with_a_take_profit_is_edited_not_doubled(tmp_path):
    d = FakeDelta()
    book = _book(tmp_path, d)
    await _open(book)
    st = book._load_state()
    st["positions"]["XRPUSD"].pop("exchange_target")               # records from before 30 Oct had only "target"
    book._save_state(st)
    await book.on_target_change("xrpusdt", 1.165, "swing_donchian@4h")
    assert [k for k, _ in d.calls].count("tp") == 1 and d.calls[-1][0] == "edit"


@pytest.mark.asyncio
async def test_a_failed_edit_places_the_new_target_before_cancelling_the_old(tmp_path):
    d = FakeDelta(fail_edit=True)
    book = _book(tmp_path, d)
    await _open(book)
    old = book._load_state()["positions"]["XRPUSD"]["tp_order_id"]
    await book.on_target_change("xrpusdt", 1.165, "swing_donchian@4h")
    assert [k for k, _ in d.calls][-2:] == ["tp", "cancel"] and d.calls[-1][1]["id"] == old
