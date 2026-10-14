"""14 Oct 2026 owner: strong-confidence intraday trades in paper, 1 hour minimum, own slots, margin cap."""
from types import SimpleNamespace

import pytest

from scheduler.runner import _family_may_trade, _tf_minutes


@pytest.fixture
def rules(monkeypatch):
    from config.settings import settings
    for k, v in dict(paper_open_families="swing,intraday", intraday_min_confidence=0.85,
                     intraday_min_timeframe_minutes=60, intraday_max_open=2, intraday_margin_cap_frac=0.10).items():
        monkeypatch.setattr(settings, k, v)
    return settings


def _sig(conf, tf, st="confluence"):
    return SimpleNamespace(confidence=conf, timeframe=tf, signal_type=st)


def test_only_strong_signals_of_an_hour_or_more_trade(rules):
    assert _family_may_trade(_sig(0.9, "1h"))
    assert _family_may_trade(_sig(0.86, "127m"))
    assert not _family_may_trade(_sig(0.9, "16m"))          # 15-minute style: never
    assert not _family_may_trade(_sig(0.8, "4h"))           # not strong enough
    assert _family_may_trade(_sig(0.5, "4h", "swing_donchian"))   # swing is not affected


def test_timeframe_parsing():
    assert (_tf_minutes("16m"), _tf_minutes("1h"), _tf_minutes("4h"), _tf_minutes("1d"), _tf_minutes("?")) == \
        (16, 60, 240, 1440, 0)


def _state(positions, wallet=5000.0):
    from analysis.paper_cycle import CycleState
    return CycleState(cycle_id=1, wallet=wallet, peak_wallet=wallet, positions=positions, position_ids={})


def test_swing_positions_do_not_use_intraday_slots_and_margin_is_capped(rules):
    from datetime import UTC, datetime

    from analysis.crypto_signal import CryptoSignal
    from analysis.paper_cycle import CycleConfig, _capped_margin, should_open
    cfg = CycleConfig(starting_wallet=5000, target_wallet=1e7, min_confidence=0.7)
    sig = CryptoSignal(symbol="ethusdt", signal_type="confluence", direction="long", trigger_description="",
                       confidence=0.9, current_price=100.0, target_price=103.0, stop_loss=98.0, edge_pct=0.0,
                       stake_pct=0.0, timeframe="1h", sentiment_score=0.0, indicators_summary="",
                       timestamp=datetime.now(UTC))
    swing = [SimpleNamespace(symbol=f"s{i}usdt", trade_mode="swing", margin=750.0) for i in range(4)]
    ok, why = should_open(sig, cfg, _state(swing, 2000.0), datetime.now(UTC))
    assert ok, why                                          # 4 swing trades open: intraday still has its slots
    assert _capped_margin(cfg, _state(swing, 2000.0), sig) <= 0.10 * (2000 + 3000) + 1e-9
    two_intra = swing + [SimpleNamespace(symbol=f"i{i}usdt", trade_mode="intraday", margin=100.0) for i in range(2)]
    assert should_open(sig, cfg, _state(two_intra, 1800.0), datetime.now(UTC)) == (False, "max_concurrent")


def test_intraday_switches_are_editable_on_the_settings_page(monkeypatch):
    from config.overrides import BY_KEY, apply
    from config.settings import settings
    for k in ("paper_open_families", "intraday_min_confidence", "intraday_min_timeframe_minutes",
              "intraday_max_open", "intraday_margin_cap_frac"):
        assert k in BY_KEY, k
    monkeypatch.setattr(settings, "paper_open_families", "swing")
    assert "paper_open_families" in apply(settings, {"paper_open_families": "swing,intraday"})
    assert settings.paper_open_families == "swing,intraday"
