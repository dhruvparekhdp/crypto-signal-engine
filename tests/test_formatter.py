from datetime import UTC, datetime

from analysis.signal import Signal
from notifications.formatter import format_signal


def _make_signal(**kwargs) -> Signal:
    defaults = dict(
        match_id="test-1",
        signal_type="momentum",
        player_to_back=2,
        player_name="Alcaraz",
        opponent_name="Djokovic",
        trigger_description="Alcaraz won last 4 games. Odds moved only 6%.",
        confidence=0.75,
        recommended_market="next_game",
        current_odds=1.72,
        fair_odds=1.55,
        edge_pct=0.109,
        stake_pct=0.012,
        tournament="Madrid Open",
        surface="clay",
        score_summary="3-6, 2-2 sets",
        match_duration_mins=83,
        timestamp=datetime.now(UTC),
    )
    defaults.update(kwargs)
    return Signal(**defaults)


def test_format_contains_player_name():
    sig = _make_signal()
    msg = format_signal(sig)
    assert "Alcaraz" in msg


def test_format_contains_odds():
    sig = _make_signal()
    msg = format_signal(sig)
    # MarkdownV2 escapes dots, so 1.72 becomes 1\.72 in the output
    assert "1\\.72" in msg


def test_format_contains_confidence():
    sig = _make_signal()
    msg = format_signal(sig)
    assert "75%" in msg


def test_format_contains_market():
    sig = _make_signal()
    msg = format_signal(sig)
    assert "Next Game" in msg


def test_all_signal_types_format():
    for stype in ("momentum", "odds_value", "serve_degradation", "set_pattern", "fatigue"):
        sig = _make_signal(signal_type=stype)
        msg = format_signal(sig)
        assert len(msg) > 100
