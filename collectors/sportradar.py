"""
Sportradar live summaries collector — tennis + soccer.

Free 30-day trial: 1,000 calls per product per rolling 30 days.
At 2-minute poll interval: ~720 calls/30 days — within trial quota.

Sign up at: https://developer.sportradar.com/
Set SPORTRADAR_API_KEY in environment.

Tennis endpoint: /tennis/trial/v3/en/schedules/live/summaries.json
Soccer endpoint:  /soccer/trial/v4/en/schedules/live/summaries.json
  → One call returns ALL live matches across every competition.
  → Covers ATP Challengers (Bengaluru etc.), WTA, ITF, all football leagues.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog

from analysis.football_state import FootballMatchState, FootballStateStore
from analysis.match_state import MatchState, OddsPoint, ServeStats
from analysis.state_store import MatchStateStore

log = structlog.get_logger()

_BASE = "https://api.sportradar.com"

_SURFACE_MAP = {
    "clay": "clay",
    "grass": "grass",
    "hard": "hard",
    "indoor_hard": "indoor_hard",
    "hard_indoor": "indoor_hard",
    "carpet": "indoor_hard",
}

_TENNIS_LIVE_URL = f"{_BASE}/tennis/trial/v3/en/schedules/live/summaries.json"
_TENNIS_SCHEDULE_URL = f"{_BASE}/tennis/trial/v3/en/schedules/{{date}}/schedule.json"
_SOCCER_LIVE_URL = f"{_BASE}/soccer/trial/v4/en/schedules/live/summaries.json"


def _header(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key}


class SportradarCollector:
    """
    Fetches live match data from Sportradar for both tennis and football.
    One HTTP call per sport covers all competitions globally.
    """

    def __init__(
        self,
        tennis_store: MatchStateStore,
        football_store: FootballStateStore,
    ) -> None:
        self.tennis_store = tennis_store
        self.football_store = football_store
        self._consecutive_failures = 0

    # ── Tennis ────────────────────────────────────────────────────────────────

    async def fetch_tennis(self, api_key: str) -> None:
        """
        Fetch live summaries from Sportradar (live only — no schedule endpoint).

        NOTE: Schedule endpoint disabled to prevent quota burn + 429 rate limits.
        Upcoming matches come from SportsData.io, API-Tennis, and ESPN instead.
        Live-only: ~288 calls/day fits within the 1,000/month free trial quota.
        """
        live_ids: set[str] = set()

        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await client.get(_TENNIS_LIVE_URL, headers=_header(api_key))
                if resp.status_code == 401:
                    log.error("sportradar_tennis_unauthorized",
                              hint="Check SPORTRADAR_API_KEY")
                    self._consecutive_failures += 1
                    return
                if resp.status_code == 403:
                    log.error("sportradar_tennis_forbidden",
                              hint="Trial expired or quota exceeded")
                    self._consecutive_failures += 1
                    return
                if resp.status_code == 200:
                    for summary in resp.json().get("summaries", []):
                        try:
                            state = await self._parse_tennis(summary, is_scheduled=False)
                            if state:
                                await self.tennis_store.update(state)
                                live_ids.add(state.match_id)
                        except Exception:
                            log.exception("sportradar_tennis_parse_error",
                                          event=summary.get("sport_event", {}).get("id", "?"))
                else:
                    log.warning("sportradar_tennis_bad_status", status=resp.status_code)
                    self._consecutive_failures += 1
            except Exception:
                self._consecutive_failures += 1
                log.exception("sportradar_tennis_fetch_failed")

        # Remove stale sr_ states not in today's live+scheduled set
        for s in await self.tennis_store.get_all():
            if s.match_id.startswith("sr_") and s.match_id not in live_ids:
                await self.tennis_store.remove(s.match_id)

        self._consecutive_failures = 0
        log.info("sportradar_tennis_done", tracked=len(live_ids))

    async def _parse_tennis_scheduled(self, sport_event: dict) -> MatchState | None:
        """Parse a sport_event from the daily schedule (upcoming match)."""
        from datetime import timezone as _tz
        from dateutil import parser as _dtparser

        status = sport_event.get("status", "")
        if status in ("closed", "ended", "cancelled"):
            return None

        match_id = f"sr_{sport_event.get('id', '').replace(':', '_')}"

        competitors = sport_event.get("competitors", [])
        if len(competitors) < 2:
            return None

        home = next((c for c in competitors if c.get("qualifier") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("qualifier") == "away"), competitors[1])
        p1_name = home.get("name", "Unknown")
        p2_name = away.get("name", "Unknown")
        if "/" in p1_name or "/" in p2_name:
            return None

        tournament_obj = sport_event.get("tournament") or sport_event.get("season") or {}
        tournament = tournament_obj.get("name", "Unknown Tournament")
        venue = sport_event.get("venue") or {}
        surface_raw = (venue.get("surface") or "hard").lower().replace(" ", "_")
        surface = _SURFACE_MAP.get(surface_raw, "hard")

        start_time: datetime | None = None
        raw_start = sport_event.get("start_time") or sport_event.get("scheduled")
        if raw_start:
            try:
                start_time = _dtparser.parse(raw_start)
                if start_time.tzinfo is None:
                    start_time = start_time.replace(tzinfo=_tz.utc)
            except Exception:
                pass

        existing = await self.tennis_store.get(match_id)
        log.info("sportradar_scheduled", match_id=match_id, p1=p1_name, p2=p2_name,
                 tournament=tournament, start=raw_start)
        return MatchState(
            match_id=match_id,
            player1_name=p1_name,
            player2_name=p2_name,
            surface=surface,
            tournament=tournament,
            current_server=0,
            sets_p1=0, sets_p2=0,
            games_in_set_p1=0, games_in_set_p2=0,
            current_set=1,
            is_tiebreak=False,
            serve_stats_p1=ServeStats(),
            serve_stats_p2=ServeStats(),
            odds_p1=existing.odds_p1 if existing else 0.0,
            odds_p2=existing.odds_p2 if existing else 0.0,
            odds_history=existing.odds_history.copy() if existing else [],
            game_log=[],
            match_duration_mins=0,
            timestamp=datetime.now(UTC),
            is_scheduled=True,
            start_time=start_time,
        )

    async def _parse_tennis(self, summary: dict, is_scheduled: bool = False) -> MatchState | None:
        event = summary.get("sport_event", {})
        status = summary.get("sport_event_status", {})
        stats_block = summary.get("statistics", {})

        # Only process live matches
        if status.get("status") not in ("live", "inprogress"):
            return None

        match_id = f"sr_{event.get('id', '').replace(':', '_')}"

        competitors = event.get("competitors", [])
        if len(competitors) < 2:
            return None

        # Sportradar doubles: >2 competitors OR "double"/"mixed" in tournament/category
        tournament_obj = event.get("tournament") or event.get("season") or {}
        tournament = tournament_obj.get("name", "Unknown Tournament")
        category_name = (
            event.get("sport_event_context", {})
            .get("category", {})
            .get("name", "")
        ).lower()
        if len(competitors) > 2:
            log.debug("sportradar_skipped_doubles_count", competitors=len(competitors),
                      tournament=tournament)
            return None
        if any(kw in tournament.lower() for kw in ("double", "dbl", "mixed")):
            log.debug("sportradar_skipped_doubles_tournament", tournament=tournament)
            return None
        if any(kw in category_name for kw in ("double", "dbl", "mixed")):
            log.debug("sportradar_skipped_doubles_category", category=category_name)
            return None

        home = next((c for c in competitors if c.get("qualifier") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("qualifier") == "away"), competitors[1])

        p1_name = home.get("name", "Unknown")
        p2_name = away.get("name", "Unknown")

        # Skip doubles where names contain "/" (some APIs use this format)
        if "/" in p1_name or "/" in p2_name:
            return None
        venue = event.get("venue") or {}
        surface_raw = (venue.get("surface") or "hard").lower().replace(" ", "_")
        surface = _SURFACE_MAP.get(surface_raw, "hard")

        # Sets score
        sets_p1 = int(status.get("home_score", 0) or 0)
        sets_p2 = int(status.get("away_score", 0) or 0)

        # Current set games from period_scores
        period_scores = status.get("period_scores", [])
        games_p1, games_p2 = 0, 0
        if period_scores:
            current = period_scores[-1]
            games_p1 = int(current.get("home_score", 0) or 0)
            games_p2 = int(current.get("away_score", 0) or 0)

        current_set = len(period_scores) if period_scores else sets_p1 + sets_p2 + 1

        # Current server (1 = home/p1, 2 = away/p2)
        server_qualifier = status.get("current_server", "")
        current_server = 1 if server_qualifier == "home" else (2 if server_qualifier == "away" else 0)

        # Is tiebreak
        game_state = status.get("game_state") or {}
        is_tiebreak = (
            games_p1 >= 6 and games_p2 >= 6 and abs(games_p1 - games_p2) < 2
        ) or game_state.get("tie_break", False)

        # Serve stats from statistics block
        serve_p1 = ServeStats()
        serve_p2 = ServeStats()
        totals = (stats_block.get("totals") or {}).get("competitors", [])
        for comp_stat in totals:
            qualifier = comp_stat.get("qualifier", "")
            s = comp_stat.get("statistics", {})
            target = serve_p1 if qualifier == "home" else serve_p2
            fsp = s.get("first_serve_percentage")
            if fsp is not None:
                target.first_serve_pct = float(fsp) / 100.0
            target.aces = int(s.get("aces", 0) or 0)
            target.double_faults = int(s.get("double_faults", 0) or 0)

        # Game log: carry from existing state + infer from score delta
        existing = await self.tennis_store.get(match_id)
        game_log: list[int] = existing.game_log.copy() if existing else []
        if existing:
            prev = existing.games_in_set_p1 + existing.games_in_set_p2
            curr = games_p1 + games_p2
            if curr > prev:
                if games_p1 > existing.games_in_set_p1:
                    game_log.append(1)
                elif games_p2 > existing.games_in_set_p2:
                    game_log.append(2)

        odds_history = existing.odds_history.copy() if existing else []

        log.info("sportradar_tennis_live",
                 match_id=match_id,
                 p1=p1_name, p2=p2_name,
                 sets=f"{sets_p1}-{sets_p2}",
                 games=f"{games_p1}-{games_p2}",
                 tournament=tournament)

        return MatchState(
            match_id=match_id,
            player1_name=p1_name,
            player2_name=p2_name,
            surface=surface,
            tournament=tournament,
            current_server=current_server,
            sets_p1=sets_p1,
            sets_p2=sets_p2,
            games_in_set_p1=games_p1,
            games_in_set_p2=games_p2,
            current_set=current_set,
            is_tiebreak=is_tiebreak,
            serve_stats_p1=serve_p1,
            serve_stats_p2=serve_p2,
            odds_p1=existing.odds_p1 if existing else 0.0,
            odds_p2=existing.odds_p2 if existing else 0.0,
            odds_history=odds_history,
            game_log=game_log,
            match_duration_mins=0,
            timestamp=datetime.now(UTC),
        )

    # ── Soccer ────────────────────────────────────────────────────────────────

    async def fetch_soccer(self, api_key: str) -> None:
        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await client.get(_SOCCER_LIVE_URL, headers=_header(api_key))
                if resp.status_code == 401:
                    log.error("sportradar_soccer_unauthorized")
                    return
                if resp.status_code == 403:
                    log.error("sportradar_soccer_forbidden",
                              hint="Trial expired or quota exceeded")
                    return
                if resp.status_code != 200:
                    log.warning("sportradar_soccer_bad_status", status=resp.status_code)
                    return
                data = resp.json()
            except Exception:
                log.exception("sportradar_soccer_fetch_failed")
                return

        summaries = data.get("summaries", [])
        live_ids: set[str] = set()

        for summary in summaries:
            try:
                state = self._parse_soccer(summary)
                if state:
                    await self.football_store.update(state)
                    live_ids.add(state.match_id)
            except Exception:
                log.exception("sportradar_soccer_parse_error",
                              event=summary.get("sport_event", {}).get("id", "?"))

        # Remove finished matches
        for s in await self.football_store.get_all():
            if s.match_id.startswith("sr_fb_") and s.match_id not in live_ids:
                await self.football_store.remove(s.match_id)

        log.info("sportradar_soccer_done", live=len(live_ids))

    def _parse_soccer(self, summary: dict) -> FootballMatchState | None:
        event = summary.get("sport_event", {})
        status = summary.get("sport_event_status", {})

        if status.get("status") not in ("live", "inprogress"):
            return None

        match_id = f"sr_fb_{event.get('id', '').replace(':', '_')}"

        competitors = event.get("competitors", [])
        if len(competitors) < 2:
            return None

        home = next((c for c in competitors if c.get("qualifier") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("qualifier") == "away"), competitors[1])

        home_team = home.get("name", "Unknown")
        away_team = away.get("name", "Unknown")

        home_score = int(status.get("home_score", 0) or 0)
        away_score = int(status.get("away_score", 0) or 0)

        # Match minute from clock
        clock = status.get("clock") or {}
        try:
            minute = int(float(clock.get("played", "0").split(":")[0] if ":" in str(clock.get("played", "0")) else clock.get("played", 0)))
        except (ValueError, TypeError):
            minute = 0

        match_status = status.get("match_status", "")
        is_halftime = match_status in ("halftime", "half_time")
        period = status.get("period", 1)
        is_extra_time = period > 2
        if is_halftime:
            minute = 45

        tournament_obj = event.get("tournament") or {}
        tournament = tournament_obj.get("name", "Unknown League")
        league_key = tournament_obj.get("id", "").replace(":", "_")

        log.info("sportradar_soccer_live",
                 match_id=match_id,
                 home=home_team, away=away_team,
                 score=f"{home_score}-{away_score}",
                 minute=minute,
                 tournament=tournament)

        return FootballMatchState(
            match_id=match_id,
            home_team=home_team,
            away_team=away_team,
            tournament=tournament,
            league_key=league_key,
            minute=minute,
            home_score=home_score,
            away_score=away_score,
            is_halftime=is_halftime,
            is_extra_time=is_extra_time,
            period=period,
            is_scheduled=False,
            timestamp=datetime.now(UTC),
        )
