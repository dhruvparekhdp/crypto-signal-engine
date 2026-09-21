"""
ESPN public tennis API collector — fallback when Sofascore is blocked.

ESPN exposes a public scoreboard API used by their own website.
No authentication, not blocked from cloud IPs, updated every ~30s.
Covers ATP, WTA, and Grand Slams.

Limitations vs Sofascore:
- No serve stats (1st serve %, aces, double faults)
- No live odds
- Score data is slightly less granular (no point-level)
"""
from datetime import UTC, datetime, timezone
from dateutil import parser as _dtparser

import httpx
import structlog

from analysis.match_state import MatchState, ServeStats
from analysis.state_store import MatchStateStore
from collectors.base import BaseCollector

log = structlog.get_logger()

# Main tours + secondary circuits + Grand Slam specific endpoints.
# Each Grand Slam has its own scoreboard that covers qualifying + main draw.
# Endpoints returning 4xx are silently skipped so adding extras is free.
_TOUR_URLS = [
    # Main tours (one featured tournament per tour at a time)
    "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard",
    "https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard",
    "https://site.api.espn.com/apis/site/v2/sports/tennis/atp-challenger/scoreboard",
    "https://site.api.espn.com/apis/site/v2/sports/tennis/wta-125/scoreboard",
    # Grand Slam specific endpoints — covers qualifying + main draw
    "https://site.api.espn.com/apis/site/v2/sports/tennis/french-open/scoreboard",
    "https://site.api.espn.com/apis/site/v2/sports/tennis/wimbledon/scoreboard",
    "https://site.api.espn.com/apis/site/v2/sports/tennis/us-open/scoreboard",
    "https://site.api.espn.com/apis/site/v2/sports/tennis/australian-open/scoreboard",
]

_SURFACE_MAP = {
    "clay": "clay",
    "grass": "grass",
    "hard": "hard",
    "indoor hard": "indoor_hard",
    "carpet": "indoor_hard",
}


class ESPNCollector(BaseCollector):
    """
    Fallback collector using ESPN's public tennis scoreboard API.
    Activated automatically by the scheduler when Sofascore is unavailable.
    """

    def __init__(self, store: MatchStateStore) -> None:
        self.store = store

    async def fetch(self) -> None:
        events: list[dict] = []
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        async with httpx.AsyncClient(timeout=15.0) as client:
            for url in _TOUR_URLS:
                try:
                    resp = await client.get(url, params={"dates": today, "limit": "100"})
                    tour_name = url.split("/tennis/")[1].split("/")[0]
                    if resp.status_code >= 400:
                        log.debug("espn_tour_skipped", tour=tour_name, status=resp.status_code)
                        continue
                    data = resp.json()
                    tour_events = data.get("events", [])
                    events.extend(tour_events)
                    log.info("espn_fetched", tour=tour_name, count=len(tour_events))
                except Exception:
                    log.exception("espn_fetch_failed", url=url)

        # Log all statuses for diagnostics
        status_counts: dict[str, int] = {}
        for event in events:
            s = event.get("status", {}).get("type", {}).get("name", "UNKNOWN")
            status_counts[s] = status_counts.get(s, 0) + 1
        if status_counts:
            log.info("espn_event_statuses", counts=status_counts)

        live_ids: set[str] = set()
        for event in events:
            try:
                state = await self._parse_event(event)
                if state:
                    await self.store.update(state)
                    live_ids.add(state.match_id)
            except Exception:
                log.exception("espn_parse_failed", event_id=event.get("id"))

        # Remove finished matches
        for state in await self.store.get_all():
            if state.match_id.startswith("espn_") and state.match_id not in live_ids:
                await self.store.remove(state.match_id)

        live_count = sum(1 for s in await self.store.get_all()
                         if s.match_id in live_ids and not s.is_scheduled)
        sched_count = len(live_ids) - live_count
        log.info("espn_collector_done", live_matches=live_count, scheduled=sched_count)

    _LIVE_STATUSES = {
        "STATUS_IN_PROGRESS", "STATUS_LIVE", "STATUS_PLAY",
        "STATUS_HALFTIME", "STATUS_OVERTIME",
        "IN_PROGRESS", "LIVE", "PLAYING",
    }
    _SCHEDULED_STATUSES = {
        "STATUS_SCHEDULED", "STATUS_PRE", "SCHEDULED", "PRE_GAME",
        "STATUS_POSTPONED", "STATUS_DELAYED",
    }
    _FINAL_STATUSES = {
        "STATUS_FINAL", "STATUS_FULL_TIME", "FINAL", "COMPLETED",
        "STATUS_RETIRED", "STATUS_WALKOVER",
    }

    async def _parse_event(self, event: dict) -> MatchState | None:
        status_obj = event.get("status", {}).get("type", {})
        status_type = status_obj.get("name", "")
        status_detail = status_obj.get("description", "")

        is_live = status_type in self._LIVE_STATUSES
        is_scheduled = status_type in self._SCHEDULED_STATUSES

        if not is_live and not is_scheduled:
            log.debug(
                "espn_event_skipped",
                event_id=event.get("id"),
                name=event.get("name", "")[:60],
                status=status_type,
                detail=status_detail,
            )
            return None

        match_id = f"espn_{event.get('id', '')}"
        competitions = event.get("competitions", [])
        if not competitions:
            log.info("espn_parse_no_competitions", event_id=event.get("id"),
                     name=event.get("name", "")[:60])
            return None

        comp = competitions[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            log.info("espn_parse_not_enough_competitors", event_id=event.get("id"),
                     name=event.get("name", "")[:60], n=len(competitors))
            return None

        # Log full structure of first live event so we can see exact field layout
        log.info("espn_live_event_raw",
                 event_id=event.get("id"),
                 name=event.get("name", "")[:60],
                 comp0_keys=list(comp.keys()),
                 cmp0_keys=list(competitors[0].keys()),
                 cmp0_score=competitors[0].get("score"),
                 cmp0_athlete=bool(competitors[0].get("athlete")),
                 cmp0_linescores=competitors[0].get("linescores", [])[:3])

        # ESPN puts home first; player name may be under athlete or directly on competitor
        def _player_name(comp: dict) -> str:
            return (
                comp.get("athlete", {}).get("displayName")
                or comp.get("displayName")
                or comp.get("team", {}).get("displayName")
                or "Unknown"
            )

        home = _player_name(competitors[0])
        away = _player_name(competitors[1])

        # Skip doubles matches — names contain "/" when two players form a team
        if "/" in home or "/" in away:
            log.info("espn_event_skipped_doubles", event_id=event.get("id"),
                     home=home[:40], away=away[:40])
            return None

        tournament = event.get("name", "Unknown Tournament")

        # Surface — ESPN sometimes includes venue surface
        venue = comp.get("venue", {})
        surface_raw = venue.get("grass", False)
        if surface_raw:
            surface = "grass"
        else:
            surface_name = venue.get("surface", "hard").lower()
            surface = _SURFACE_MAP.get(surface_name, "hard")

        # Score — ESPN provides linescores per set
        home_linescores = competitors[0].get("linescores", [])
        away_linescores = competitors[1].get("linescores", [])

        home_sets = int(competitors[0].get("score", "0") or 0)
        away_sets = int(competitors[1].get("score", "0") or 0)

        current_set_idx = len(home_linescores) - 1
        if current_set_idx < 0:
            current_set_idx = 0

        games_p1 = 0
        games_p2 = 0
        if home_linescores and current_set_idx < len(home_linescores):
            games_p1 = int(home_linescores[current_set_idx].get("value", 0) or 0)
        if away_linescores and current_set_idx < len(away_linescores):
            games_p2 = int(away_linescores[current_set_idx].get("value", 0) or 0)

        current_set = home_sets + away_sets + 1

        # Parse start time for scheduled matches
        start_time: datetime | None = None
        if is_scheduled:
            raw_date = event.get("date") or comp.get("date")
            if raw_date:
                try:
                    start_time = _dtparser.parse(raw_date)
                    if start_time.tzinfo is None:
                        start_time = start_time.replace(tzinfo=timezone.utc)
                except Exception:
                    pass

        # Carry over game_log from previous state and infer new game winner from score delta
        existing = await self.store.get(match_id)
        game_log: list[int] = existing.game_log.copy() if existing else []
        if existing:
            prev_total = existing.games_in_set_p1 + existing.games_in_set_p2
            curr_total = games_p1 + games_p2
            if curr_total > prev_total:
                if games_p1 > existing.games_in_set_p1:
                    game_log.append(1)
                elif games_p2 > existing.games_in_set_p2:
                    game_log.append(2)

        return MatchState(
            match_id=match_id,
            player1_name=home,
            player2_name=away,
            surface=surface,
            tournament=tournament,
            current_server=0,   # ESPN doesn't expose current server
            sets_p1=home_sets,
            sets_p2=away_sets,
            games_in_set_p1=games_p1,
            games_in_set_p2=games_p2,
            current_set=current_set,
            is_tiebreak=games_p1 >= 6 and games_p2 >= 6 and abs(games_p1 - games_p2) < 2,
            serve_stats_p1=ServeStats(),   # ESPN doesn't provide serve stats
            serve_stats_p2=ServeStats(),
            odds_p1=0.0,
            odds_p2=0.0,
            odds_history=[],
            game_log=game_log,
            match_duration_mins=0,
            timestamp=datetime.now(UTC),
            is_scheduled=is_scheduled,
            start_time=start_time,
        )
