"""
SportsData.io Tennis collector — free trial (250 requests/day).

Sign up at https://www.sportsdata.io/ → Tennis API
Free trial gives 250 API calls/day with live + scheduled matches.

Endpoints:
  GET /api/v1/Tennis/Matches
  → Returns all matches with live scores, status, odds

Cloud-safe: works from Render without IP blocking.
"""
from __future__ import annotations

from datetime import UTC, datetime, timezone

import httpx
import structlog

from analysis.match_state import MatchState, ServeStats
from analysis.state_store import MatchStateStore
from collectors.base import BaseCollector
from config.settings import settings

log = structlog.get_logger()

_BASE = "https://api.sportsdata.io/v1/tennis"


def _is_doubles(p1: str, p2: str, tournament: str = "") -> bool:
    if "/" in p1 or "/" in p2:
        return True
    t = tournament.lower()
    return "double" in t


_SURFACE_MAP = {
    "clay": "clay",
    "grass": "grass",
    "hard": "hard",
    "carpet": "indoor_hard",
    "indoor": "indoor_hard",
}


class SportsDataCollector(BaseCollector):
    """Live + scheduled tennis from SportsData.io (250 req/day free trial)."""

    def __init__(self, store: MatchStateStore) -> None:
        self.store = store
        self._consecutive_failures = 0
        self.quota_remaining: int | None = None
        self.quota_total: int = 250
        self.last_live_count: int = 0
        self.last_scheduled_count: int = 0

    async def fetch(self) -> None:
        key = settings.sportsdata_api_key
        if not key:
            return

        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await client.get(
                    f"{_BASE}/matches",
                    params={
                        "key": key,
                        "format": "json",
                    },
                )

                if resp.status_code == 401:
                    log.error("sportsdata_unauthorized")
                    self._consecutive_failures += 1
                    return
                if resp.status_code == 429:
                    log.warning("sportsdata_quota_exceeded")
                    return
                if resp.status_code != 200:
                    log.warning("sportsdata_error", status=resp.status_code)
                    self._consecutive_failures += 1
                    return

                # Try to extract quota from response headers
                remaining = resp.headers.get("x-api-remaining")
                if remaining:
                    try:
                        self.quota_remaining = int(remaining)
                    except ValueError:
                        pass

                self._consecutive_failures = 0
                data = resp.json()
                matches = data if isinstance(data, list) else data.get("matches", [])

                fetched_ids: set[str] = set()
                live_count = 0
                scheduled_count = 0

                for m in matches:
                    try:
                        state = self._parse_match(m)
                        if state:
                            await self.store.update(state)
                            fetched_ids.add(state.match_id)
                            if state.is_scheduled:
                                scheduled_count += 1
                            else:
                                live_count += 1
                    except Exception:
                        log.exception("sportsdata_parse_failed", match_id=m.get("MatchId"))

                # Remove old matches
                for existing in await self.store.get_all():
                    if (existing.match_id.startswith("sd_")
                            and existing.match_id not in fetched_ids):
                        await self.store.remove(existing.match_id)

                self.last_live_count = live_count
                self.last_scheduled_count = scheduled_count
                log.info("sportsdata_done", live=live_count, scheduled=scheduled_count,
                         quota_remaining=self.quota_remaining)
            except Exception:
                self._consecutive_failures += 1
                log.exception("sportsdata_fetch_failed",
                              consecutive=self._consecutive_failures)

    def _parse_match(self, m: dict) -> MatchState | None:
        match_id = m.get("MatchId")
        if not match_id:
            return None

        match_id = f"sd_{match_id}"

        p1 = m.get("Player1Name", "Unknown")
        p2 = m.get("Player2Name", "Unknown")
        tournament = m.get("Tournament", {})
        tournament_name = tournament.get("Name", "Unknown")

        if _is_doubles(p1, p2, tournament_name):
            return None

        # Status: NotStarted, InProgress, Completed, Cancelled, Postponed
        status = m.get("Status", "")
        is_live = status == "InProgress"
        is_scheduled = status == "NotStarted"

        if not (is_live or is_scheduled):
            return None

        # Score
        sets_p1 = 0
        sets_p2 = 0
        games_p1 = 0
        games_p2 = 0
        current_set = 1

        if is_live:
            # Parse set scores if available
            sets_obj = m.get("Sets", [])
            for s in sets_obj:
                p1_games = s.get("Player1Games", 0)
                p2_games = s.get("Player2Games", 0)
                if p1_games > p2_games:
                    sets_p1 += 1
                elif p2_games > p1_games:
                    sets_p2 += 1

            # Current set games
            if sets_obj:
                curr = sets_obj[-1]
                games_p1 = curr.get("Player1Games", 0)
                games_p2 = curr.get("Player2Games", 0)
                current_set = len(sets_obj)

        # Surface from tournament
        surface_raw = tournament.get("Surface", "hard").lower()
        surface = _SURFACE_MAP.get(surface_raw, "hard")

        # Start time for scheduled matches
        start_time: datetime | None = None
        if is_scheduled:
            date_str = m.get("Day")
            if date_str:
                try:
                    start_time = datetime.fromisoformat(
                        date_str.replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

        return MatchState(
            match_id=match_id,
            player1_name=p1,
            player2_name=p2,
            surface=surface,
            tournament=tournament_name,
            current_server=0,
            sets_p1=sets_p1,
            sets_p2=sets_p2,
            games_in_set_p1=games_p1,
            games_in_set_p2=games_p2,
            current_set=current_set,
            is_tiebreak=(
                games_p1 >= 6 and games_p2 >= 6 and abs(games_p1 - games_p2) < 2
            ),
            serve_stats_p1=ServeStats(),
            serve_stats_p2=ServeStats(),
            odds_p1=0.0,
            odds_p2=0.0,
            odds_history=[],
            game_log=[],
            match_duration_mins=0,
            timestamp=datetime.now(UTC),
            is_scheduled=is_scheduled,
            start_time=start_time,
        )
