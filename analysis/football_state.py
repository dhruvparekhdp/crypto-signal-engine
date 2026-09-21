from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class FootballOddsPoint:
    home_odds: float
    draw_odds: float
    away_odds: float
    minute: int
    timestamp: datetime


@dataclass
class FootballMatchState:
    match_id: str
    home_team: str
    away_team: str
    tournament: str
    league_key: str
    minute: int          # 0–90+ (includes stoppage time)
    home_score: int
    away_score: int
    home_odds: float = 0.0
    draw_odds: float = 0.0
    away_odds: float = 0.0
    odds_history: list[FootballOddsPoint] = field(default_factory=list)
    home_red_cards: int = 0
    away_red_cards: int = 0
    is_halftime: bool = False
    is_extra_time: bool = False
    period: int = 1      # 1=first half, 2=second half, 3+=ET
    is_scheduled: bool = False   # True = upcoming, not yet live
    kickoff_time: datetime | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def goal_diff(self) -> int:
        """Positive = home winning."""
        return self.home_score - self.away_score

    @property
    def total_goals(self) -> int:
        return self.home_score + self.away_score

    def leader(self) -> str | None:
        if self.home_score > self.away_score:
            return self.home_team
        if self.away_score > self.home_score:
            return self.away_team
        return None

    def leader_is_home(self) -> bool:
        return self.home_score > self.away_score


class FootballStateStore:
    def __init__(self) -> None:
        self._states: dict[str, FootballMatchState] = {}
        self._lock = asyncio.Lock()

    async def update(self, state: FootballMatchState) -> None:
        async with self._lock:
            existing = self._states.get(state.match_id)
            if existing:
                # Preserve odds history and carry over odds if new state has none
                state.odds_history = existing.odds_history.copy()
                if state.home_odds == 0.0:
                    state.home_odds = existing.home_odds
                    state.draw_odds = existing.draw_odds
                    state.away_odds = existing.away_odds
            self._states[state.match_id] = state

    async def update_odds(
        self,
        match_id: str,
        home_odds: float,
        draw_odds: float,
        away_odds: float,
        minute: int,
    ) -> None:
        async with self._lock:
            state = self._states.get(match_id)
            if state:
                state.home_odds = home_odds
                state.draw_odds = draw_odds
                state.away_odds = away_odds
                state.odds_history.append(FootballOddsPoint(
                    home_odds=home_odds,
                    draw_odds=draw_odds,
                    away_odds=away_odds,
                    minute=minute,
                    timestamp=datetime.now(UTC),
                ))
                state.odds_history = state.odds_history[-50:]

    async def get(self, match_id: str) -> FootballMatchState | None:
        return self._states.get(match_id)

    async def get_all(self) -> list[FootballMatchState]:
        return list(self._states.values())

    async def remove(self, match_id: str) -> None:
        self._states.pop(match_id, None)

    async def count(self) -> int:
        return len(self._states)
