"""
API-Tennis collector — free WebSocket + REST API (no credit limit).

Sign up at https://api-tennis.com/documentation
Uses REST (no WebSocket polling overhead) for live + scheduled matches.

Endpoints:
  GET /events/?apikey=KEY&season_id=2026&...
  → Live and upcoming tennis matches from multiple tours

Cloud-safe: works from Render without IP blocking.
No hard credit limits — can poll frequently.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import httpx
import structlog

from analysis.match_state import MatchState, ServeStats
from analysis.state_store import MatchStateStore
from collectors.base import BaseCollector
from config.settings import settings

log = structlog.get_logger()

_BASE = "https://api.api-tennis.com/v3"


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


class ApiTennisCollector(BaseCollector):
    """Live + scheduled tennis from api-tennis.com (no hard credit limits)."""

    def __init__(self, store: MatchStateStore) -> None:
        self.store = store
        self._consecutive_failures = 0
        self.last_live_count: int = 0
        self.last_scheduled_count: int = 0

    async def fetch(self) -> None:
        key = settings.api_tennis_key
        if not key:
            return

        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                # Get current season (2026)
                now = datetime.now(timezone.utc)
                year = now.year

                # Fetch live events
                resp = await client.get(
                    f"{_BASE}/events",
                    params={
                        "apikey": key,
                        "season_id": year,
                        "is_live": "1",
                    },
                )

                if resp.status_code == 401:
                    log.error("api_tennis_unauthorized")
                    self._consecutive_failures += 1
                    return
                if resp.status_code != 200:
                    log.warning("api_tennis_error", status=resp.status_code)
                    self._consecutive_failures += 1
                    return

                self._consecutive_failures = 0
                data = resp.json()
                events = data.get("response", [])

                fetched_ids: set[str] = set()
                live_count = 0
                scheduled_count = 0

                for event in events:
                    try:
                        state = self._parse_event(event, is_live=True)
                        if state:
                            await self.store.update(state)
                            fetched_ids.add(state.match_id)
                            live_count += 1
                    except Exception:
                        log.exception("api_tennis_parse_failed",
                                    event_id=event.get("event_id"))

                # Also fetch upcoming (scheduled) for next 48h
                tomorrow = (now + timedelta(days=2)).strftime("%Y-%m-%d")
                try:
                    resp2 = await client.get(
                        f"{_BASE}/events",
                        params={
                            "apikey": key,
                            "season_id": year,
                            "date_start": now.strftime("%Y-%m-%d"),
                            "date_end": tomorrow,
                        },
                    )
                    if resp2.status_code == 200:
                        data2 = resp2.json()
                        upcoming = data2.get("response", [])
                        for event in upcoming:
                            try:
                                state = self._parse_event(event, is_live=False)
                                if state and state.match_id not in fetched_ids:
                                    await self.store.update(state)
                                    fetched_ids.add(state.match_id)
                                    scheduled_count += 1
                            except Exception:
                                pass
                except Exception:
                    pass

                # Remove old matches
                for existing in await self.store.get_all():
                    if (existing.match_id.startswith("at_")
                            and existing.match_id not in fetched_ids):
                        await self.store.remove(existing.match_id)

                self.last_live_count = live_count
                self.last_scheduled_count = scheduled_count
                log.info("api_tennis_done", live=live_count, scheduled=scheduled_count)
            except Exception:
                self._consecutive_failures += 1
                log.exception("api_tennis_fetch_failed",
                              consecutive=self._consecutive_failures)

    def _parse_event(self, event: dict, is_live: bool = False) -> MatchState | None:
        event_id = event.get("event_id")
        if not event_id:
            return None

        match_id = f"at_{event_id}"

        # Get player names
        p1_obj = event.get("home", {})
        p2_obj = event.get("away", {})
        p1 = p1_obj.get("name", "Unknown")
        p2 = p2_obj.get("name", "Unknown")

        league = event.get("league", {})
        tournament = league.get("name", "Unknown")

        if _is_doubles(p1, p2, tournament):
            return None

        # Status: Not Started, Live, Finished, etc.
        status_obj = event.get("status", {})
        status = status_obj.get("short", "")

        if is_live and status not in ("LIVE", "1ST", "2ND", "3RD", "4TH", "5TH"):
            return None

        # Score
        sets_p1 = 0
        sets_p2 = 0
        games_p1 = 0
        games_p2 = 0
        current_set = 1

        if is_live:
            statistics = event.get("statistics", {})
            p1_stats = statistics.get("home", {})
            p2_stats = statistics.get("away", {})

            # Set scores
            sets_p1 = int(p1_stats.get("sets_won", 0))
            sets_p2 = int(p2_stats.get("sets_won", 0))

            # Games in current set
            games_p1 = int(p1_stats.get("games_in_current_set", 0))
            games_p2 = int(p2_stats.get("games_in_current_set", 0))

            current_set = sets_p1 + sets_p2 + 1

        # Surface — infer from tournament name
        surface = "hard"
        t_lower = tournament.lower()
        if any(x in t_lower for x in ["clay", "roland", "madrid", "rome", "monte"]):
            surface = "clay"
        elif any(x in t_lower for x in ["grass", "wimbledon", "halle", "queen"]):
            surface = "grass"

        # Start time for scheduled
        start_time: datetime | None = None
        date_start = event.get("date_start")
        if date_start and not is_live:
            try:
                start_time = datetime.fromisoformat(
                    date_start.replace("Z", "+00:00")
                )
            except ValueError:
                pass

        return MatchState(
            match_id=match_id,
            player1_name=p1,
            player2_name=p2,
            surface=surface,
            tournament=tournament,
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
            timestamp=datetime.now(timezone.utc),
            is_scheduled=not is_live,
            start_time=start_time,
        )
