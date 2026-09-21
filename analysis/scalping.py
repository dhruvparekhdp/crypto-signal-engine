"""
Scalping / sure-shot winner detection for live tennis.

A "scalp" here = a near-certain in-play winner: the favourite holds a structurally
decisive lead AND the market prices them at very short odds, so backing them carries
minimal risk for a small, near-locked return.

Parimatch (and most books the user uses) are fixed-odds, not exchanges, so true
back/lay scalping isn't possible. Instead we:
  1. Flag matches that are effectively decided ("lock" / "strong" / "watch" tiers).
  2. Detect a "scalp window" — when the favourite's odds momentarily drift UP after
     dropping a game, giving a slightly better entry before they close the match out.

This engine is data-source independent: it runs on any MatchState that has a score
(and ideally live odds), so it works on Odds API, Sportradar, ESPN, pushed
Flashscore, or pushed Parimatch data alike.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from analysis.match_state import MatchState
from analysis.win_probability import compute_win_probability

_GRAND_SLAMS = frozenset({
    "australian open", "roland garros", "french open", "wimbledon", "us open",
})

# Simulated / virtual tennis leagues — never real money opportunities.
_ETENNIS_MARKERS = ("etennis", "e-tennis", "esports", "virtual", "cyber", "esoccer")

# Player name patterns that indicate a simulated/bot player rather than a real person.
# Parimatch eTennis uses names like "Alcaraz (Glory)", "mACEsman9", "Djokovic (Fire)".
_ETENNIS_NAME_RE = re.compile(
    r"\(Glory\)|\(Fire\)|\(Storm\)|\(Ice\)|mACE|Cyber|Bot\d|Player\d",
    re.IGNORECASE,
)


def _is_etennis(state: MatchState) -> bool:
    """True if the match is a simulated/virtual eTennis event — skip for scalping."""
    t = state.tournament.lower()
    if any(m in t for m in _ETENNIS_MARKERS):
        return True
    for name in (state.player1_name, state.player2_name):
        if _ETENNIS_NAME_RE.search(name):
            return True
        # Numeric suffixes like "mACEsman9" or all-lowercase "xgamer" patterns
        if re.search(r"\d{1,3}$", name) and not re.search(r"[A-Z]", name[1:]):
            return True
    return False


@dataclass
class ScalpOpportunity:
    match_id: str
    winner: int                 # 1 or 2 — the near-certain winner
    player_name: str            # favourite
    opponent_name: str
    tournament: str
    surface: str
    source: str                 # data-source prefix (pm, fs, sr, odds, espn…)
    score_summary: str          # e.g. "6-2, 4-1 *" (* = favourite serving)
    win_prob: float             # model probability favourite wins, 0–1
    market_odds: float          # favourite back odds (0 if unknown)
    market_implied: float       # 1 / market_odds (0 if unknown)
    edge_pct: float             # (win_prob − market_implied) × 100
    ev_pct: float               # (win_prob × market_odds − 1) × 100
    tier: str                   # "lock" | "strong" | "watch"
    reasons: list[str] = field(default_factory=list)
    scalp_window: bool = False  # odds drifted up → better entry now
    is_serving: bool = False    # favourite currently serving
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


def _best_of(state: MatchState) -> int:
    t = state.tournament.lower()
    if any(s in t for s in _GRAND_SLAMS):
        return 5
    if state.sets_p1 + state.sets_p2 >= 3:
        return 5
    return 3


def _score_summary(state: MatchState, fav: int) -> str:
    """Human-readable score from the favourite's perspective with a serve marker."""
    # Per-set reconstruction is complex; show completed sets count + current games.
    sets = f"{state.sets_p1}-{state.sets_p2} sets"
    games = f"{state.games_in_set_p1}-{state.games_in_set_p2}"
    marker = ""
    if state.current_server == fav:
        marker = " *"  # favourite serving
    elif state.current_server != 0:
        marker = " ·"  # opponent serving
    tb = " (TB)" if state.is_tiebreak else ""
    return f"{sets}, {games}{tb}{marker}"


def _dominance(state: MatchState, fav: int) -> tuple[list[str], int]:
    """Return (human reasons, dominance score 0–100) describing how decided the lead is."""
    fav_sets = state.sets_p1 if fav == 1 else state.sets_p2
    opp_sets = state.sets_p2 if fav == 1 else state.sets_p1
    fav_games = state.games_in_set_p1 if fav == 1 else state.games_in_set_p2
    opp_games = state.games_in_set_p2 if fav == 1 else state.games_in_set_p1
    best_of = _best_of(state)
    sets_to_win = (best_of + 1) // 2

    reasons: list[str] = []
    score = 0

    set_lead = fav_sets - opp_sets
    if set_lead >= 1:
        reasons.append(f"{set_lead} set{'s' if set_lead > 1 else ''} up")
        score += 32 * set_lead

    # One set away from victory
    if fav_sets == sets_to_win - 1:
        reasons.append("one set from the match")
        score += 14

    break_lead = fav_games - opp_games
    if break_lead >= 2 and fav_games >= 3:
        reasons.append(f"+{break_lead} games this set")
        score += 22
    elif break_lead >= 1 and fav_games >= 4:
        reasons.append("a break up this set")
        score += 11

    # Serving for the match: leading set(s), one set from win, ahead & serving at 5+
    serving_for_match = (
        state.current_server == fav
        and fav_sets == sets_to_win - 1
        and fav_games >= 5
        and break_lead >= 1
    )
    if serving_for_match:
        reasons.append("serving for the match")
        score += 26

    # Recent game streak from game_log
    streak = state.consecutive_games_won_by(fav)
    if streak >= 3:
        reasons.append(f"{streak} games in a row")
        score += min(streak * 3, 15)

    return reasons, min(score, 100)


def _scalp_window(state: MatchState, fav: int, lookback: int = 8,
                  min_drift: float = 0.04) -> bool:
    """True if the favourite's odds drifted UP from their recent low (better entry)."""
    hist = state.odds_history[-lookback:]
    series = [
        (h.odds_p1 if fav == 1 else h.odds_p2)
        for h in hist
        if (h.odds_p1 if fav == 1 else h.odds_p2) > 1.01
    ]
    if len(series) < 2:
        return False
    cur = state.odds_p1 if fav == 1 else state.odds_p2
    if cur <= 1.01:
        return False
    recent_low = min(series)
    return cur > recent_low * (1 + min_drift)


def detect(
    state: MatchState,
    win_p1: float | None = None,
    win_p2: float | None = None,
    *,
    min_win_prob: float = 0.90,
    lock_win_prob: float = 0.97,
    max_odds: float = 1.25,
    lock_max_odds: float = 1.10,
) -> ScalpOpportunity | None:
    """
    Detect whether `state` is a sure-shot / scalp opportunity.

    Fires if EITHER the model is highly confident (win_prob ≥ min_win_prob) OR the
    market prices the favourite very short (odds ≤ max_odds) — but always requires a
    real structural lead so we never flag a noisy 0-0 start.
    """
    if state.is_scheduled:
        return None

    if _is_etennis(state):
        return None

    # Skip matches that have barely started (no meaningful lead yet)
    if (state.sets_p1 == 0 and state.sets_p2 == 0
            and abs(state.games_in_set_p1 - state.games_in_set_p2) <= 1):
        return None

    if win_p1 is None or win_p2 is None:
        win_p1, win_p2 = compute_win_probability(state)

    fav = 1 if win_p1 >= win_p2 else 2
    win_p = max(win_p1, win_p2)
    odds = state.odds_p1 if fav == 1 else state.odds_p2
    fav_name = state.player1_name if fav == 1 else state.player2_name
    opp_name = state.player2_name if fav == 1 else state.player1_name

    has_odds = odds > 1.01
    implied = (1.0 / odds) if has_odds else 0.0

    model_ok = win_p >= min_win_prob
    market_ok = has_odds and odds <= max_odds
    if not (model_ok or market_ok):
        return None

    reasons, dom = _dominance(state, fav)
    # Require a genuine structural lead unless the market is screaming short odds.
    if dom < 30 and not (has_odds and odds <= lock_max_odds):
        return None

    edge = (win_p - implied) * 100 if has_odds else 0.0
    ev = (win_p * odds - 1.0) * 100 if has_odds else 0.0

    if win_p >= lock_win_prob and (not has_odds or odds <= lock_max_odds):
        tier = "lock"
    elif model_ok and (not has_odds or odds <= max_odds):
        tier = "strong"
    else:
        tier = "watch"

    source = state.match_id.split("_")[0]

    return ScalpOpportunity(
        match_id=state.match_id,
        winner=fav,
        player_name=fav_name,
        opponent_name=opp_name,
        tournament=state.tournament,
        surface=state.surface,
        source=source,
        score_summary=_score_summary(state, fav),
        win_prob=win_p,
        market_odds=odds if has_odds else 0.0,
        market_implied=implied,
        edge_pct=round(edge, 1),
        ev_pct=round(ev, 1),
        tier=tier,
        reasons=reasons,
        scalp_window=_scalp_window(state, fav),
        is_serving=(state.current_server == fav),
        timestamp=datetime.now(UTC),
    )


_TIER_RANK = {"lock": 0, "strong": 1, "watch": 2}


def scan_all(
    states: list[MatchState],
    **thresholds,
) -> list[ScalpOpportunity]:
    """Run detection across all live states; return opportunities sorted best-first."""
    out: list[ScalpOpportunity] = []
    for s in states:
        try:
            opp = detect(s, **thresholds)
            if opp:
                out.append(opp)
        except Exception:
            continue
    out.sort(key=lambda o: (_TIER_RANK.get(o.tier, 3), -o.win_prob))
    return out
