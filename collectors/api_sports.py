"""
API-Sports Tennis collector — 100 req/day FREE (permanent), cloud-safe.

Sign up at https://dashboard.api-football.com/register (same platform).
Set API_SPORTS_KEY in your environment.

Endpoints used:
  GET /games?live=all         → all live tennis matches right now
  GET /games?date=YYYY-MM-DD  → today's scheduled matches
  GET /odds?game={id}         → bookmaker odds for a match (costs extra quota)

Quota usage:
  - /games live  = 1 req  (covers ALL live matches)
  - /games date  = 1 req  (covers ALL scheduled today)
  - /odds        = 1 req per match (skip if quota is tight)

With 100 req/day and polling every 15 min (96 polls/day), we skip odds fetching
to stay within the free quota. Odds from Odds API will already be in the store.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import httpx
import structlog

from analysis.match_state import MatchState, OddsPoint, ServeStats
from analysis.state_store import MatchStateStore
from collectors.base import BaseCollector
from config.settings import settings

log = structlog.get_logger()

_BASE = "https://v1.tennis.api-sports.io"

_SURFACE_MAP: dict[str, str] = {
    "clay": "clay",
    "grass": "grass",
    "hard": "hard",
    "carpet": "indoor_hard",
    "indoor": "indoor_hard",
    "indoor hard": "indoor_hard",
    "acrylic": "hard",
}

_LIVE_STATUSES = {
    "In Progress",
    "1st Set",
    "2nd Set",
    "3rd Set",
    "4th Set",
    "5th Set",
    "Tie-Break",
    "Break",
    "Suspended",
}

_FINISHED_STATUSES = {
    "Finished",
    "After Retirement",
    "Walkover",
    "Cancelled",
    "Postponed",
    "Abandoned",
}


def _surface_from_tournament(name: str) -> str:
    name_l = name.lower()
    for key, val in _SURFACE_MAP.items():
        if key in name_l:
            return val
    # Known surface assignments for major tournaments
    if any(x in name_l for x in ("roland garros", "french open", "monte carlo",
                                   "monte-carlo", "madrid", "rome", "internazionali",
                                   "clay")):
        return "clay"
    if any(x in name_l for x in ("wimbledon", "queen", "halle", "grass",
                                   "eastbourne", "s-hertogenbosch")):
        return "grass"
    return "hard"


def _count_sets(scores: dict) -> tuple[int, int]:
    """Count completed sets won by home/away from the nested scores dict."""
    sets_h = sets_a = 0
    for set_num in ("1", "2", "3", "4", "5"):
        h = scores.get("home", {}).get(set_num)
        a = scores.get("away", {}).get(set_num)
        if h is None or a is None:
            break
        if h > a:
            sets_h += 1
        elif a > h:
            sets_a += 1
    return sets_h, sets_a


def _current_games(scores: dict) -> tuple[int, int]:
    """Get games in the current (highest non-null) set."""
    last_h = last_a = 0
    for set_num in ("1", "2", "3", "4", "5"):
        h = scores.get("home", {}).get(set_num)
        a = scores.get("away", {}).get(set_num)
        if h is None or a is None:
            break
        last_h, last_a = h, a
    return last_h, last_a


class ApiSportsCollector(BaseCollector):
    """
    Live + upcoming tennis from API-Sports — genuinely free, cloud-safe.
    Uses minimal quota: 2 requests per poll (live + today's schedule).
    """

    def __init__(self, store: MatchStateStore) -> None:
        self.store = store
        self._consecutive_failures = 0
        self.quota_remaining: int | None = None
        self.last_live_count: int = 0
        self.last_scheduled_count: int = 0

    async def fetch(self) -> None:
        key = settings.api_sports_key
        if not key:
            return  # silently skip — not configured

        headers = {
            "x-apisports-key": key,
            "x-apisports-host": "v1.tennis.api-sports.io",
        }

        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            try:
                live_states = await self._fetch_live(client)
                scheduled_states = await self._fetch_scheduled(client)
                self._consecutive_failures = 0
            except Exception:
                self._consecutive_failures += 1
                log.exception("api_sports_fetch_failed",
                              consecutive=self._consecutive_failures)
                return

        all_fetched_ids: set[str] = set()
        for state in live_states + scheduled_states:
            await self.store.update(state)
            all_fetched_ids.add(state.match_id)

        # Remove matches that are no longer returned (finished/cancelled)
        for existing in await self.store.get_all():
            if (existing.match_id.startswith("apisports_")
                    and existing.match_id not in all_fetched_ids):
                await self.store.remove(existing.match_id)

        self.last_live_count = len(live_states)
        self.last_scheduled_count = len(scheduled_states)
        log.info("api_sports_done",
                 live=self.last_live_count,
                 scheduled=self.last_scheduled_count,
                 quota_remaining=self.quota_remaining)

    async def _fetch_live(self, client: httpx.AsyncClient) -> list[MatchState]:
        resp = await client.get(f"{_BASE}/games", params={"live": "all"})
        self._update_quota(resp)

        if resp.status_code == 401:
            log.error("api_sports_unauthorized")
            return []
        if resp.status_code != 200:
            log.warning("api_sports_live_error", status=resp.status_code,
                        body=resp.text[:200])
            return []

        games: list[dict] = resp.json().get("response", [])
        results: list[MatchState] = []
        for g in games:
            state = self._parse_game(g, is_scheduled=False)
            if state:
                results.append(state)
        return results

    async def _fetch_scheduled(self, client: httpx.AsyncClient) -> list[MatchState]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        resp = await client.get(f"{_BASE}/games", params={"date": today})
        self._update_quota(resp)

        if resp.status_code != 200:
            log.warning("api_sports_scheduled_error", status=resp.status_code)
            return []

        games: list[dict] = resp.json().get("response", [])
        now = datetime.now(timezone.utc)
        results: list[MatchState] = []
        for g in games:
            status_long = g.get("status", {}).get("long", "")
            if status_long in _FINISHED_STATUSES:
                continue
            if status_long in _LIVE_STATUSES:
                continue  # already captured by live endpoint
            # Accept scheduled/not started
            state = self._parse_game(g, is_scheduled=True)
            if state and state.start_time:
                # Only show matches starting in next 24h
                if state.start_time > now + timedelta(hours=24):
                    continue
                results.append(state)
        return results

    def _parse_game(self, game: dict, is_scheduled: bool) -> MatchState | None:
        game_id = game.get("id")
        if not game_id:
            return None

        teams = game.get("teams", {})
        home_name: str = teams.get("home", {}).get("name", "Unknown")
        away_name: str = teams.get("away", {}).get("name", "Unknown")

        if not home_name or not away_name or home_name == "Unknown":
            return None

        # Skip doubles (names containing "/")
        if "/" in home_name or "/" in away_name:
            return None

        tournament_data = game.get("tournament", {})
        tournament_name: str = tournament_data.get("name", "Unknown Tournament")
        category = tournament_data.get("category", {}).get("name", "")
        if category:
            tournament_name = f"{tournament_name} ({category})"

        surface = _surface_from_tournament(tournament_name)

        # Parse score
        scores = game.get("scores", {})
        sets_h, sets_a = _count_sets(scores)
        games_h, games_a = _current_games(scores)
        current_set = sets_h + sets_a + 1

        # Parse start time
        start_time: datetime | None = None
        date_str: str | None = game.get("date")
        if date_str:
            try:
                start_time = datetime.fromisoformat(
                    date_str.replace("Z", "+00:00")
                )
            except ValueError:
                pass

        # Carry forward game_log and odds_history from existing state
        match_id = f"apisports_{game_id}"
        existing = None
        # We can't do async here; existing state merging done via store.update()
        # The store.update() call will merge if we implement it — for now start fresh
        # (odds_history accumulates via Odds API collector writing to same store key)

        return MatchState(
            match_id=match_id,
            player1_name=home_name,
            player2_name=away_name,
            surface=surface,
            tournament=tournament_name,
            current_server=0,
            sets_p1=sets_h,
            sets_p2=sets_a,
            games_in_set_p1=games_h,
            games_in_set_p2=games_a,
            current_set=current_set,
            is_tiebreak=(
                games_h >= 6 and games_a >= 6 and abs(games_h - games_a) < 2
            ),
            serve_stats_p1=ServeStats(),
            serve_stats_p2=ServeStats(),
            odds_p1=0.0,
            odds_p2=0.0,
            odds_history=[],
            game_log=[],
            match_duration_mins=0,
            timestamp=datetime.now(timezone.utc),
            is_scheduled=is_scheduled,
            start_time=start_time,
        )

    def _update_quota(self, resp: httpx.Response) -> None:
        remaining = resp.headers.get("x-ratelimit-requests-remaining")
        if remaining is not None:
            try:
                self.quota_remaining = int(remaining)
            except ValueError:
                pass
