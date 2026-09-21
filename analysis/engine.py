from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from analysis.break_momentum import BreakMomentumAnalyzer
from analysis.endgame import EndgameAnalyzer
from analysis.fatigue import FatigueAnalyzer
from analysis.match_state import MatchState
from analysis.ml_predictor import MLPredictor
from analysis.momentum import MomentumAnalyzer
from analysis.odds_value import OddsValueAnalyzer
from analysis.second_set import SecondSetFadeAnalyzer
from analysis.server_performance import ServerPerformanceAnalyzer
from analysis.set_patterns import SetPatternAnalyzer
from analysis.signal import Signal
from analysis.win_probability import model_fair_odds
from config.settings import is_tier1, settings
from storage.models import PlayerStats
from storage.repository import Repository

log = structlog.get_logger()


class AnalysisEngine:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.momentum = MomentumAnalyzer()
        self.odds_value = OddsValueAnalyzer()
        self.server_perf = ServerPerformanceAnalyzer()
        self.set_patterns = SetPatternAnalyzer()
        self.fatigue = FatigueAnalyzer()
        self.endgame = EndgameAnalyzer()
        self.break_momentum = BreakMomentumAnalyzer()
        self.second_set = SecondSetFadeAnalyzer()
        self.ml = MLPredictor()
        # In-memory cooldown cache: (match_id, signal_type) → last_sent datetime
        self._cooldowns: dict[tuple[str, str], datetime] = {}

    async def process(self, state: MatchState) -> list[Signal]:
        """Run all analyzers against the given match state, return signals that pass
        confidence threshold and cooldown checks."""
        if settings.tournament_tier == "tier1" and not is_tier1(state.tournament):
            log.debug("tournament_filtered", tournament=state.tournament, match_id=state.match_id)
            return []

        player_stats = await self._load_player_stats(state)

        p1_prob, p2_prob = self.ml.predict(state)
        model_p1_fair, model_p2_fair = model_fair_odds(state)

        candidates: list[Signal | None] = [
            self.momentum.analyze(state),
            self.odds_value.analyze(state),
            self.server_perf.analyze(state),
            self.set_patterns.analyze(state, player_stats),
            self.fatigue.analyze(state),
            self.endgame.analyze(state),
            self.break_momentum.analyze(state),
            self.second_set.analyze(state),
        ]

        # ML value signal: fire when model probability differs from market by >15%
        # Min odds 1.20 per player — don't second-guess near-certainties
        if state.odds_p1 > 1.20 and state.odds_p2 > 1.20:
            for player, model_prob, market_odds, player_name, opponent_name in [
                (1, p1_prob, state.odds_p1, state.player1_name, state.player2_name),
                (2, p2_prob, state.odds_p2, state.player2_name, state.player1_name),
            ]:
                market_prob = 1.0 / market_odds
                edge = model_prob - market_prob
                if edge > 0.15:
                    fair_odds = round(1.0 / model_prob, 3) if model_prob > 0 else 999.0
                    edge_pct = edge
                    confidence = min(0.5 + edge * 2, 0.85)
                    from analysis.signal import compute_stake
                    stake_pct = compute_stake(edge_pct, market_odds)
                    candidates.append(Signal(
                        match_id=state.match_id,
                        signal_type="ml_value",
                        player_to_back=player,
                        player_name=player_name,
                        opponent_name=opponent_name,
                        trigger_description=(
                            f"ML model gives {model_prob:.1%} win prob vs "
                            f"market implied {market_prob:.1%} (edge {edge:.1%})"
                        ),
                        confidence=confidence,
                        recommended_market="match_winner",
                        current_odds=market_odds,
                        fair_odds=fair_odds,
                        edge_pct=edge_pct,
                        stake_pct=stake_pct,
                        tournament=state.tournament,
                        surface=state.surface,
                        score_summary=(
                            f"{state.sets_p1}-{state.sets_p2}, "
                            f"{state.games_in_set_p1}-{state.games_in_set_p2}"
                        ),
                        match_duration_mins=state.match_duration_mins,
                        timestamp=state.timestamp,
                    ))

        fired: list[Signal] = []
        for sig in candidates:
            if sig is None:
                continue
            if sig.confidence < settings.min_confidence:
                log.debug("signal_below_threshold",
                          signal_type=sig.signal_type,
                          confidence=sig.confidence,
                          match=f"{sig.player_name} vs {sig.opponent_name}")
                continue
            if self._is_on_cooldown(state.match_id, sig.signal_type):
                log.debug("signal_on_cooldown", signal_type=sig.signal_type, match_id=state.match_id)
                continue

            self._set_cooldown(state.match_id, sig.signal_type)
            await self._log_signal(sig, state)
            fired.append(sig)

        return fired

    def _is_on_cooldown(self, match_id: str, signal_type: str) -> bool:
        key = (match_id, signal_type)
        last = self._cooldowns.get(key)
        if last is None:
            return False
        cooldown = timedelta(minutes=settings.signal_cooldown_minutes)
        return datetime.now(UTC) - last < cooldown

    def _set_cooldown(self, match_id: str, signal_type: str) -> None:
        self._cooldowns[(match_id, signal_type)] = datetime.now(UTC)

    async def _log_signal(self, sig: Signal, state: MatchState) -> None:
        try:
            # fair_odds already encodes the model's win probability for the backed player
            model_prob = round(1.0 / sig.fair_odds, 4) if sig.fair_odds > 1.0 else 0.0
            await self.repository.log_signal(
                match_id=sig.match_id,
                signal_type=sig.signal_type,
                player_to_back=sig.player_to_back,
                trigger_description=sig.trigger_description,
                confidence=sig.confidence,
                recommended_market=sig.recommended_market,
                current_odds=sig.current_odds,
                fair_odds=sig.fair_odds,
                edge_pct=sig.edge_pct,
                stake_pct=sig.stake_pct,
                player_name=sig.player_name,
                opponent_name=sig.opponent_name,
                tournament=sig.tournament,
                surface=sig.surface,
                model_win_prob=model_prob,
                score_at_signal=f"{state.sets_p1}-{state.sets_p2}, {state.games_in_set_p1}-{state.games_in_set_p2}",
                sets_p1=state.sets_p1,
                sets_p2=state.sets_p2,
                games_p1=state.games_in_set_p1,
                games_p2=state.games_in_set_p2,
            )
        except Exception:
            log.exception("failed_to_log_signal", signal_type=sig.signal_type)

    async def _load_player_stats(self, state: MatchState) -> dict[str, PlayerStats | None]:
        stats: dict[str, PlayerStats | None] = {}
        for name in (state.player1_name, state.player2_name):
            try:
                stats[name] = await self.repository.get_player_stats(name, state.surface)
            except Exception:
                stats[name] = None
        return stats
