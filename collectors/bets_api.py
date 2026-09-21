"""
BetsAPI live tennis collector — cloud-safe primary data source.

BetsAPI (betsapi.com) is a commercial API designed for server-side use.
It provides live scores, in-play odds, and match stats without blocking
cloud IPs like Flashscore/Sofascore do.

Sign up at https://betsapi.com — plans from $10/month.
Set BETS_API_TOKEN in your environment.

Endpoints used:
  GET /v3/events/inplay   → live matches (sport_id=13 = tennis)
  GET /v3/event/view      → match detail (scores per set)
  GET /v3/event/odds/summary → bookmaker odds
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog

from analysis.match_state import MatchState, OddsPoint, ServeStats
from analysis.state_store import MatchStateStore
from collectors.base import BaseCollector
from config.settings import settings

log = structlog.get_logger()

_BASE = "https://api.b365api.com"
_TENNIS_SPORT_ID = 13

_SURFACE_MAP = {
    "clay": "clay",
    "grass": "grass",
    "hard": "hard",
    "carpet": "indoor_hard",
    "indoor": "indoor_hard",
    "indoor hard": "indoor_hard",
}


def _parse_score(ss: str) -> tuple[int, int, int, int]:
    """
    Parse BetsAPI ss field e.g. '1-0,3-2' → (sets_home, sets_away, games_home, games_away).
    First part = sets, second part = current games.
    """
    try:
        parts = ss.split(",")
        sets_h, sets_a = (int(x) for x in parts[0].split("-")) if parts else (0, 0)
        games_h, games_a = (int(x) for x in parts[1].split("-")) if len(parts) > 1 else (0, 0)
        return sets_h, sets_a, games_h, games_a
    except Exception:
        return 0, 0, 0, 0


def _best_odds(odds_data: dict, side: str) -> float:
    """Extract best decimal back price for 'home' or 'away' from odds summary."""
    best = 0.0
    for book in odds_data.get("results", []):
        try:
            val = float(book.get(side, 0) or 0)
            if val > 1.0 and (best == 0.0 or val < best):
                best = val
        except (ValueError, TypeError):
            pass
    return best


class BetsAPICollector(BaseCollector):
    """
    Live tennis data from BetsAPI — works reliably from cloud IPs.
    Provides scores, sets breakdown, and in-play odds.
    """

    def __init__(self, store: MatchStateStore) -> None:
        self.store = store
        self._consecutive_failures = 0

    async def fetch(self) -> None:
        token = settings.bets_api_token
        if not token:
            return  # silently skip — token not configured

        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await client.get(
                    f"{_BASE}/v3/events/inplay",
                    params={"sport_id": _TENNIS_SPORT_ID, "token": token},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                self._consecutive_failures += 1
                log.exception("betsapi_inplay_failed",
                              consecutive=self._consecutive_failures)
                return

            events = data.get("results", [])
            log.info("betsapi_live_fetched", count=len(events))
            self._consecutive_failures = 0

            live_ids: set[str] = set()
            for event in events:
                try:
                    state = await self._parse_event(event, client, token)
                    if state:
                        await self.store.update(state)
                        live_ids.add(state.match_id)
                except Exception:
                    log.exception("betsapi_parse_failed", event_id=event.get("id"))

            # Remove finished matches
            for state in await self.store.get_all():
                if state.match_id.startswith("bets_") and state.match_id not in live_ids:
                    await self.store.remove(state.match_id)

            log.info("betsapi_collector_done", live_matches=len(live_ids))

    async def _parse_event(
        self, event: dict, client: httpx.AsyncClient, token: str
    ) -> MatchState | None:
        event_id = event.get("id", "")
        match_id = f"bets_{event_id}"

        teams = event.get("team", [])
        if len(teams) < 2:
            return None

        home_name = teams[0].get("name", "Unknown")
        away_name = teams[1].get("name", "Unknown")

        # Skip doubles
        if "/" in home_name or "/" in away_name:
            return None

        # Score — ss field format: "sets_home-sets_away,games_home-games_away"
        ss = event.get("ss", "") or ""
        sets_h, sets_a, games_h, games_a = _parse_score(ss)
        current_set = sets_h + sets_a + 1

        # Surface from tournament name heuristic
        league = event.get("league", {}).get("name", "").lower()
        surface = "hard"
        for key, val in _SURFACE_MAP.items():
            if key in league:
                surface = val
                break

        tournament = event.get("league", {}).get("name", "Unknown Tournament")

        # Fetch odds (separate request — costs quota)
        odds_p1, odds_p2 = 0.0, 0.0
        try:
            odds_resp = await client.get(
                f"{_BASE}/v3/event/odds/summary",
                params={"token": token, "event_id": event_id},
            )
            if odds_resp.status_code == 200:
                odds_data = odds_resp.json()
                # h2h market (match winner)
                for book in odds_data.get("results", {}).get("bookmaker", []):
                    home_odds = float(book.get("home_od", 0) or 0)
                    away_odds = float(book.get("away_od", 0) or 0)
                    if home_odds > 1.0 and (odds_p1 == 0.0 or home_odds < odds_p1):
                        odds_p1 = home_odds
                    if away_odds > 1.0 and (odds_p2 == 0.0 or away_odds < odds_p2):
                        odds_p2 = away_odds
        except Exception:
            pass  # odds failure is non-fatal

        # Carry game_log and odds_history from previous state
        existing = await self.store.get(match_id)
        game_log: list[int] = existing.game_log.copy() if existing else []
        odds_history = existing.odds_history.copy() if existing else []

        if existing:
            prev_total = existing.games_in_set_p1 + existing.games_in_set_p2
            curr_total = games_h + games_a
            if curr_total > prev_total:
                if games_h > existing.games_in_set_p1:
                    game_log.append(1)
                elif games_a > existing.games_in_set_p2:
                    game_log.append(2)

        if odds_p1 > 1.0 and odds_p2 > 1.0:
            odds_history.append(OddsPoint(odds_p1=odds_p1, odds_p2=odds_p2,
                                          timestamp=datetime.now(UTC)))

        log.info("betsapi_live_match",
                 match_id=match_id, home=home_name, away=away_name,
                 score=ss, odds_p1=odds_p1, odds_p2=odds_p2)

        return MatchState(
            match_id=match_id,
            player1_name=home_name,
            player2_name=away_name,
            surface=surface,
            tournament=tournament,
            current_server=0,
            sets_p1=sets_h,
            sets_p2=sets_a,
            games_in_set_p1=games_h,
            games_in_set_p2=games_a,
            current_set=current_set,
            is_tiebreak=games_h >= 6 and games_a >= 6 and abs(games_h - games_a) < 2,
            serve_stats_p1=ServeStats(),
            serve_stats_p2=ServeStats(),
            odds_p1=odds_p1,
            odds_p2=odds_p2,
            odds_history=odds_history,
            game_log=game_log,
            match_duration_mins=0,
            timestamp=datetime.now(UTC),
        )
