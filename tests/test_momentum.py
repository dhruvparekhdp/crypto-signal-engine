from datetime import UTC, datetime, timedelta

from analysis.match_state import MatchState, OddsPoint, ServeStats
from analysis.momentum import MomentumAnalyzer


def _make_state(**kwargs) -> MatchState:
    defaults = dict(
        match_id="test-1",
        player1_name="Djokovic",
        player2_name="Alcaraz",
        surface="clay",
        tournament="Madrid Open",
        current_server=1,
        sets_p1=0, sets_p2=1,
        games_in_set_p1=2, games_in_set_p2=2,
        current_set=2,
        is_tiebreak=False,
        serve_stats_p1=ServeStats(),
        serve_stats_p2=ServeStats(),
        odds_p1=2.50, odds_p2=1.60,
        odds_history=[],
        game_log=[],
        match_duration_mins=45,
        timestamp=datetime.now(UTC),
    )
    defaults.update(kwargs)
    return MatchState(**defaults)


def test_no_signal_when_streak_too_short():
    state = _make_state(game_log=[1, 2, 1, 2, 1, 1])  # only 2 in a row
    sig = MomentumAnalyzer().analyze(state)
    assert sig is None


def test_signal_fires_on_3_game_streak():
    # Alcaraz (player 2) won last 3 games
    state = _make_state(
        game_log=[1, 2, 2, 2, 2, 2, 2],  # 5 consecutive for player 2
        odds_p2=1.60,
        odds_history=[
            OddsPoint(odds_p1=2.60, odds_p2=1.55, timestamp=datetime.now(UTC)),
        ],
    )
    sig = MomentumAnalyzer().analyze(state)
    assert sig is not None
    assert sig.player_to_back == 2
    assert sig.signal_type == "momentum"
    assert 0.55 <= sig.confidence <= 0.85


def test_no_signal_when_odds_already_moved():
    """Market already reflected the momentum — no edge."""
    now = datetime.now(UTC)
    old_time = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=20)
    state = _make_state(
        game_log=[1, 2, 2, 2, 2],
        odds_p2=1.30,  # odds already shortened significantly
        odds_history=[
            OddsPoint(odds_p1=1.80, odds_p2=2.10, timestamp=old_time),  # p2 was 2.10 before
            OddsPoint(odds_p1=1.50, odds_p2=1.30, timestamp=now),       # now 1.30 — moved 38%
        ],
    )
    sig = MomentumAnalyzer().analyze(state)
    # Either no signal or confidence is lower — odds moved enough to reflect momentum
    # The 38% move exceeds our 15% threshold so signal should not fire
    assert sig is None


def test_no_signal_in_tiebreak():
    state = _make_state(
        game_log=[1, 2, 1, 1, 1, 1],
        is_tiebreak=True,
    )
    sig = MomentumAnalyzer().analyze(state)
    assert sig is None


def test_stake_is_positive_when_signal_fires():
    state = _make_state(
        game_log=[2, 2, 2, 2, 2],
        odds_p2=2.00,
        odds_history=[OddsPoint(odds_p1=1.80, odds_p2=2.00, timestamp=datetime.now(UTC))],
    )
    sig = MomentumAnalyzer().analyze(state)
    if sig:
        assert sig.stake_pct > 0
        assert sig.edge_pct > 0
