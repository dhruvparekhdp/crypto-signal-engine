"""
Backtesting engine — replays Jeff Sackmann's tennis_pointbypoint data.

Downloads point-by-point CSVs from:
  https://github.com/JeffSackmann/tennis_pointbypoint
  https://github.com/JeffSackmann/tennis_slam_pointbypoint

Each row is one point. We replay game-by-game, reconstructing a MatchState
at each game boundary, then fire all signal analyzers. We record:
  - Signal fires (type, odds, confidence, edge)
  - Whether the signal was correct (backed player won the match)
  - P&L assuming flat stake = recommended stake_pct * 100 bank units

Usage:
    python -m scripts.backtest                 # ATP Grand Slams (default)
    python -m scripts.backtest --tour atp      # All ATP tours
    python -m scripts.backtest --tour wta      # WTA
    python -m scripts.backtest --year 2023     # Single year
    python -m scripts.backtest --output results.csv

Output CSV columns:
    signal_type, player_name, opponent_name, tournament, surface,
    current_set, games_p1, games_p2, sets_p1, sets_p2,
    odds_p1, odds_p2, edge_pct, confidence, stake_pct,
    player_to_back, match_winner, correct, pnl_units
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

import httpx

# Add project root to sys.path so we can import analysis modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from analysis.break_momentum import BreakMomentumAnalyzer
from analysis.endgame import EndgameAnalyzer
from analysis.match_state import MatchState, OddsPoint, ServeStats
from analysis.momentum import MomentumAnalyzer
from analysis.odds_value import OddsValueAnalyzer
from analysis.second_set import SecondSetFadeAnalyzer
from analysis.signal import Signal

_GITHUB_RAW = "https://raw.githubusercontent.com/JeffSackmann"

# Slam point-by-point (best quality, game-level resolution)
_SLAM_URLS: dict[str, str] = {
    "ausopen":     f"{_GITHUB_RAW}/tennis_slam_pointbypoint/master/{{year}}-ausopen-points.csv",
    "frenchopen":  f"{_GITHUB_RAW}/tennis_slam_pointbypoint/master/{{year}}-frenchopen-points.csv",
    "wimbledon":   f"{_GITHUB_RAW}/tennis_slam_pointbypoint/master/{{year}}-wimbledon-points.csv",
    "usopen":      f"{_GITHUB_RAW}/tennis_slam_pointbypoint/master/{{year}}-usopen-points.csv",
}

# ATP/WTA general point-by-point (fewer matches, still useful)
_ATP_URL = f"{_GITHUB_RAW}/tennis_pointbypoint/master/pbp_matches_atp_{{year}}_{{round}}.csv"
_WTA_URL = f"{_GITHUB_RAW}/tennis_pointbypoint/master/pbp_matches_wta_{{year}}_{{round}}.csv"

_SLAM_SURFACES: dict[str, str] = {
    "ausopen": "hard",
    "frenchopen": "clay",
    "wimbledon": "grass",
    "usopen": "hard",
}

_SLAM_NAMES: dict[str, str] = {
    "ausopen": "Australian Open",
    "frenchopen": "Roland Garros",
    "wimbledon": "Wimbledon",
    "usopen": "US Open",
}


@dataclass
class BacktestResult:
    signal_type: str
    player_name: str
    opponent_name: str
    tournament: str
    surface: str
    current_set: int
    sets_p1: int
    sets_p2: int
    games_p1: int
    games_p2: int
    odds_p1: float
    odds_p2: float
    edge_pct: float
    confidence: float
    stake_pct: float
    player_to_back: int
    match_winner: int
    correct: bool
    pnl_units: float  # stake_pct * 100 * (odds - 1) if correct, else -(stake_pct * 100)


@dataclass
class _MatchContext:
    """Running state for one match during replay."""
    match_id: str
    player1: str
    player2: str
    surface: str
    tournament: str
    best_of: int = 3
    sets_p1: int = 0
    sets_p2: int = 0
    games_p1: int = 0
    games_p2: int = 0
    points_p1_in_game: int = 0
    points_p2_in_game: int = 0
    game_log: list[int] = field(default_factory=list)
    winner: int = 0  # filled in when match ends


def _make_state(ctx: _MatchContext, fake_odds: tuple[float, float]) -> MatchState:
    """Build a MatchState snapshot from the current game context."""
    odds_p1, odds_p2 = fake_odds
    current_set = ctx.sets_p1 + ctx.sets_p2 + 1
    is_tiebreak = ctx.games_p1 >= 6 and ctx.games_p2 >= 6 and abs(ctx.games_p1 - ctx.games_p2) < 2
    return MatchState(
        match_id=ctx.match_id,
        player1_name=ctx.player1,
        player2_name=ctx.player2,
        surface=ctx.surface,
        tournament=ctx.tournament,
        current_server=0,
        sets_p1=ctx.sets_p1,
        sets_p2=ctx.sets_p2,
        games_in_set_p1=ctx.games_p1,
        games_in_set_p2=ctx.games_p2,
        current_set=current_set,
        is_tiebreak=is_tiebreak,
        serve_stats_p1=ServeStats(),
        serve_stats_p2=ServeStats(),
        odds_p1=odds_p1,
        odds_p2=odds_p2,
        odds_history=[
            OddsPoint(odds_p1=odds_p1, odds_p2=odds_p2, timestamp=datetime.now(UTC))
        ],
        game_log=list(ctx.game_log),
        match_duration_mins=max(10, len(ctx.game_log) * 4),
        timestamp=datetime.now(UTC),
    )


def _odds_from_model_prob(win_prob: float) -> tuple[float, float]:
    """Convert win probability to decimal odds with a 5% margin (realistic market)."""
    p1 = max(0.02, min(0.98, win_prob))
    p2 = 1.0 - p1
    margin = 0.05
    o1 = round((1.0 / (p1 * (1 + margin))), 2)
    o2 = round((1.0 / (p2 * (1 + margin))), 2)
    return o1, o2


def _replay_match(rows: list[dict], tournament: str, surface: str, best_of: int = 5) -> Iterator[tuple[MatchState, _MatchContext]]:
    """
    Replay point-by-point rows for a single match.
    Yields a (MatchState, context) tuple after each game ends.

    Slam pbp format key columns:
      match_id, set1, set2, game1, game2, point, Svr, won
      won: 1 = server won, 0 = returner won
    """
    if not rows:
        return

    match_id = rows[0].get("match_id", "unknown")
    # Player names aren't in the pbp file — use match_id components or Unknown
    player1 = rows[0].get("player1", "Player1")
    player2 = rows[0].get("player2", "Player2")

    ctx = _MatchContext(
        match_id=match_id,
        player1=player1,
        player2=player2,
        surface=surface,
        tournament=tournament,
        best_of=best_of,
    )

    sets_needed = (best_of + 1) // 2
    prev_game1, prev_game2 = -1, -1
    prev_set1, prev_set2 = 0, 0

    for row in rows:
        try:
            set1 = int(row.get("set1", 0))
            set2 = int(row.get("set2", 0))
            game1 = int(row.get("game1", 0))
            game2 = int(row.get("game2", 0))
        except (ValueError, TypeError):
            continue

        # Detect game boundary — when game counts change
        if (game1, game2) != (prev_game1, prev_game2) and prev_game1 >= 0:
            # A game just completed
            if game1 > prev_game1:
                ctx.game_log.append(1)
                ctx.games_p1 = game1
                ctx.games_p2 = game2
            elif game2 > prev_game2:
                ctx.game_log.append(2)
                ctx.games_p1 = game1
                ctx.games_p2 = game2

        # Detect set boundary
        if (set1, set2) != (prev_set1, prev_set2):
            ctx.sets_p1 = set1
            ctx.sets_p2 = set2
            ctx.games_p1 = game1
            ctx.games_p2 = game2

        # Check match over
        if ctx.sets_p1 >= sets_needed:
            ctx.winner = 1
        elif ctx.sets_p2 >= sets_needed:
            ctx.winner = 2

        # Build a fake market-implied odds using naive model
        # (In real backtest you'd use pre-match odds from a separate data source;
        #  here we use a rough estimate from match progress.)
        total_sets = ctx.sets_p1 + ctx.sets_p2 + 1
        naive_p1 = 0.5 + (ctx.sets_p1 - ctx.sets_p2) * 0.15 + (ctx.games_p1 - ctx.games_p2) * 0.02
        naive_p1 = max(0.10, min(0.90, naive_p1))
        fake_odds = _odds_from_model_prob(naive_p1)

        # Yield state after each game boundary
        if (game1, game2) != (prev_game1, prev_game2) and prev_game1 >= 0:
            state = _make_state(ctx, fake_odds)
            yield state, ctx

        prev_game1, prev_game2 = game1, game2
        prev_set1, prev_set2 = set1, set2


def _group_by_match(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        mid = row.get("match_id", "")
        if mid:
            groups[mid].append(row)
    return groups


async def _download(client: httpx.AsyncClient, url: str) -> list[dict]:
    try:
        resp = await client.get(url, timeout=60.0)
        if resp.status_code == 404:
            print(f"  [skip] not found: {url.split('/')[-1]}", file=sys.stderr)
            return []
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        return list(reader)
    except Exception as exc:
        print(f"  [error] {url.split('/')[-1]}: {exc}", file=sys.stderr)
        return []


async def run_backtest(
    years: list[int],
    slam_only: bool = True,
    output_path: str = "backtest_results.csv",
) -> None:
    analyzers = [
        MomentumAnalyzer(),
        OddsValueAnalyzer(),
        EndgameAnalyzer(),
        BreakMomentumAnalyzer(),
        SecondSetFadeAnalyzer(),
    ]

    results: list[BacktestResult] = []

    async with httpx.AsyncClient(
        headers={"User-Agent": "tennis-bet-backtest/1.0"},
        follow_redirects=True,
    ) as client:
        for year in years:
            print(f"\n=== Year {year} ===")
            urls_to_fetch: list[tuple[str, str, str, int]] = []  # (url, tournament, surface, best_of)

            if slam_only:
                for slam_key, url_tpl in _SLAM_URLS.items():
                    urls_to_fetch.append((
                        url_tpl.format(year=year),
                        _SLAM_NAMES[slam_key],
                        _SLAM_SURFACES[slam_key],
                        5,  # Grand Slams are BO5 (men)
                    ))
            # Could add ATP/WTA regular tour here

            for url, tournament, surface, best_of in urls_to_fetch:
                rows = await _download(client, url)
                if not rows:
                    continue

                matches = _group_by_match(rows)
                print(f"  {tournament}: {len(matches)} matches, {len(rows)} points")

                for match_id, match_rows in matches.items():
                    match_results: list[BacktestResult] = []
                    match_winner = 0

                    for state, ctx in _replay_match(match_rows, tournament, surface, best_of):
                        match_winner = ctx.winner

                        for analyzer in analyzers:
                            try:
                                sig = analyzer.analyze(state)
                            except Exception:
                                continue
                            if sig is None:
                                continue

                            match_results.append(BacktestResult(
                                signal_type=sig.signal_type,
                                player_name=sig.player_name,
                                opponent_name=sig.opponent_name,
                                tournament=tournament,
                                surface=surface,
                                current_set=state.current_set,
                                sets_p1=state.sets_p1,
                                sets_p2=state.sets_p2,
                                games_p1=state.games_in_set_p1,
                                games_p2=state.games_in_set_p2,
                                odds_p1=state.odds_p1,
                                odds_p2=state.odds_p2,
                                edge_pct=sig.edge_pct,
                                confidence=sig.confidence,
                                stake_pct=sig.stake_pct,
                                player_to_back=sig.player_to_back,
                                match_winner=0,  # filled below
                                correct=False,
                                pnl_units=0.0,
                            ))

                    # Fill in match winner and P&L
                    for r in match_results:
                        r.match_winner = match_winner
                        r.correct = (match_winner > 0 and r.player_to_back == match_winner)
                        bank = 100.0
                        stake = r.stake_pct * bank
                        if r.player_to_back == 1:
                            odds = r.odds_p1
                        else:
                            odds = r.odds_p2
                        r.pnl_units = stake * (odds - 1) if r.correct else -stake

                    results.extend(match_results)

    if not results:
        print("\nNo results generated. Check data availability.")
        return

    # Write CSV
    fieldnames = [
        "signal_type", "player_name", "opponent_name", "tournament", "surface",
        "current_set", "sets_p1", "sets_p2", "games_p1", "games_p2",
        "odds_p1", "odds_p2", "edge_pct", "confidence", "stake_pct",
        "player_to_back", "match_winner", "correct", "pnl_units",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow({k: getattr(r, k) for k in fieldnames})

    # Print summary
    print(f"\n{'='*60}")
    print(f"BACKTEST RESULTS — {len(results)} signals across {len(years)} year(s)")
    print(f"{'='*60}")

    by_type: dict[str, list[BacktestResult]] = defaultdict(list)
    for r in results:
        by_type[r.signal_type].append(r)

    total_pnl = 0.0
    for sig_type, sig_results in sorted(by_type.items()):
        n = len(sig_results)
        n_correct = sum(1 for r in sig_results if r.correct)
        pnl = sum(r.pnl_units for r in sig_results)
        total_pnl += pnl
        win_rate = n_correct / n * 100 if n > 0 else 0
        roi = pnl / (sum(r.stake_pct * 100 for r in sig_results)) * 100 if sig_results else 0
        print(f"  {sig_type:<25} {n:>4} signals  {win_rate:>5.1f}% win  ROI: {roi:>+7.1f}%  P&L: {pnl:>+8.2f} units")

    total_stake = sum(r.stake_pct * 100 for r in results)
    total_roi = total_pnl / total_stake * 100 if total_stake > 0 else 0
    print(f"\n  {'TOTAL':<25} {len(results):>4} signals  P&L: {total_pnl:>+8.2f} units  ROI: {total_roi:>+7.1f}%")
    print(f"\nResults written to: {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tennis-bet signal backtester")
    parser.add_argument("--year", type=int, action="append", dest="years",
                        help="Year(s) to backtest (default: 2022 2023 2024)")
    parser.add_argument("--slams-only", action="store_true", default=True,
                        help="Only use Grand Slam pbp data (default: True)")
    parser.add_argument("--output", default="backtest_results.csv",
                        help="Output CSV path (default: backtest_results.csv)")
    args = parser.parse_args()

    years = args.years or [2022, 2023, 2024]
    asyncio.run(run_backtest(years=years, slam_only=args.slams_only, output_path=args.output))


if __name__ == "__main__":
    main()
