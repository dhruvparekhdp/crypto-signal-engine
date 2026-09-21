"""
Football odds collector using The Odds API.

Covers all soccer sport keys active on The Odds API — Premier League,
Bundesliga, Serie A, La Liga, Ligue 1, MLS, etc.

Creates FootballMatchState entries for:
  - Live matches (commence_time < now)
  - Upcoming matches starting within 12 hours

Odds: 3-way h2h (home / draw / away) from EU bookmakers.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import httpx
import structlog

from analysis.football_state import FootballMatchState, FootballStateStore

log = structlog.get_logger()

_API_BASE = "https://api.the-odds-api.com/v4"
_SOON_HOURS = 12


def _best_price(bookmakers: list[dict], outcome_name: str) -> float:
    """Best (lowest) decimal back price for a named outcome across all bookmakers."""
    best = 0.0
    for bm in bookmakers:
        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for o in market.get("outcomes", []):
                if o.get("name", "") == outcome_name:
                    price = float(o.get("price", 0) or 0)
                    if price > 1.0 and (best == 0.0 or price < best):
                        best = price
    return best


def _sport_to_league_key(sport_key: str) -> str:
    """Map Odds API sport key to a short league identifier."""
    mapping = {
        "soccer_epl": "eng.1",
        "soccer_england_league1": "eng.2",
        "soccer_england_league2": "eng.3",
        "soccer_spain_la_liga": "esp.1",
        "soccer_spain_segunda_division": "esp.2",
        "soccer_germany_bundesliga": "ger.1",
        "soccer_germany_bundesliga2": "ger.2",
        "soccer_italy_serie_a": "ita.1",
        "soccer_italy_serie_b": "ita.2",
        "soccer_france_ligue_one": "fra.1",
        "soccer_france_ligue_two": "fra.2",
        "soccer_netherlands_eredivisie": "ned.1",
        "soccer_portugal_primeira_liga": "por.1",
        "soccer_turkey_super_league": "tur.1",
        "soccer_usa_mls": "usa.1",
        "soccer_mexico_ligamx": "mex.1",
        "soccer_brazil_campeonato": "bra.1",
        "soccer_argentina_primera_division": "arg.1",
        "soccer_uefa_champs_league": "uefa.champions",
        "soccer_uefa_europa_league": "uefa.europa",
        "soccer_uefa_euro_qualification": "uefa.euro_qual",
        "soccer_conmebol_copa_libertadores": "conmebol.libertadores",
        "soccer_fifa_world_cup": "fifa.world",
        "soccer_conmebol_copa_america": "conmebol.america",
        "soccer_uefa_euro": "uefa.euro",
    }
    return mapping.get(sport_key, sport_key.replace("soccer_", ""))


class FootballOddsApiCollector:
    """Fetches football odds from The Odds API and updates FootballStateStore."""

    def __init__(self, store: FootballStateStore) -> None:
        self.store = store
        self._odds_ids: set[str] = set()

    # Always-try keys even if not in the sports list (World Cup, Copa America)
    _ALWAYS_TRY = [
        "soccer_fifa_world_cup",
        "soccer_conmebol_copa_america",
        "soccer_uefa_euro",
    ]

    async def _get_active_soccer_sports(
        self, client: httpx.AsyncClient, api_key: str
    ) -> list[str]:
        try:
            resp = await client.get(f"{_API_BASE}/sports/", params={"apiKey": api_key})
            if resp.status_code != 200:
                return list(self._ALWAYS_TRY)
            all_sports: list[dict] = resp.json()
            keys = [
                s["key"]
                for s in all_sports
                if "soccer" in s.get("key", "").lower() and s.get("active", False)
            ]
            # Merge always-try keys first so WC is prioritised
            seen: set[str] = set(self._ALWAYS_TRY)
            merged = list(self._ALWAYS_TRY)
            for k in keys:
                if k not in seen:
                    seen.add(k)
                    merged.append(k)
            log.info("football_odds_api_sports", active_soccer=merged)
            return merged
        except Exception as exc:
            log.error("football_odds_api_sports_failed", error=str(exc))
            return list(self._ALWAYS_TRY)

    async def fetch(self, api_key: str) -> None:
        now = datetime.now(timezone.utc)
        commence_from = (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        commence_to = (now + timedelta(hours=_SOON_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")

        current_ids: set[str] = set()

        async with httpx.AsyncClient(timeout=15.0) as client:
            sports = await self._get_active_soccer_sports(client, api_key)
            if not sports:
                return

            for sport in sports:
                try:
                    resp = await client.get(
                        f"{_API_BASE}/sports/{sport}/odds/",
                        params={
                            "apiKey": api_key,
                            "regions": "eu",
                            "markets": "h2h",
                            "oddsFormat": "decimal",
                            "commenceTimeFrom": commence_from,
                            "commenceTimeTo": commence_to,
                        },
                    )
                    if resp.status_code != 200:
                        log.debug("football_odds_api_skip", sport=sport, status=resp.status_code)
                        continue

                    events: list[dict] = resp.json()
                    log.info("football_odds_api_fetched", sport=sport, count=len(events))

                    for event in events:
                        try:
                            match_id = await self._process_event(event, sport, now)
                            if match_id:
                                current_ids.add(match_id)
                        except Exception:
                            log.exception("football_odds_api_event_error",
                                          event_id=event.get("id"))
                except Exception:
                    log.exception("football_odds_api_sport_error", sport=sport)

        # Remove stale odds-created states
        for stale_id in self._odds_ids - current_ids:
            existing = await self.store.get(stale_id)
            if existing:
                await self.store.remove(stale_id)
        self._odds_ids = current_ids
        log.info("football_odds_api_done", tracked=len(current_ids))

    async def _process_event(
        self, event: dict, sport_key: str, now: datetime
    ) -> str | None:
        home_team = event.get("home_team", "")
        away_team = event.get("away_team", "")
        bookmakers = event.get("bookmakers", [])
        if not home_team or not away_team or not bookmakers:
            return None

        ct_str = event.get("commence_time", "")
        try:
            ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
        except Exception:
            return None

        mins_until = (ct - now).total_seconds() / 60
        is_live = mins_until <= 0
        is_upcoming = 0 < mins_until <= _SOON_HOURS * 60

        if not is_live and not is_upcoming:
            return None

        home_odds = _best_price(bookmakers, home_team)
        draw_odds = _best_price(bookmakers, "Draw")
        away_odds = _best_price(bookmakers, away_team)

        # Some bookmakers use the team names for outcomes; fall back to position
        if home_odds == 0.0 or away_odds == 0.0:
            home_odds = _best_price_by_index(bookmakers, 0)
            draw_odds = _best_price_by_index(bookmakers, 1)
            away_odds = _best_price_by_index(bookmakers, 2)

        odds_id = f"odds_fb_{event.get('id', home_team + '_' + away_team)}"
        league_key = _sport_to_league_key(sport_key)
        tournament = sport_key.replace("soccer_", "").replace("_", " ").title()

        existing = await self.store.get(odds_id)
        if existing is not None:
            # Update odds, flip to live if match started
            await self.store.update_odds(
                odds_id,
                home_odds=home_odds,
                draw_odds=draw_odds,
                away_odds=away_odds,
                minute=existing.minute,
            )
            if is_live and existing.is_scheduled:
                existing.is_scheduled = False
            return odds_id

        new_state = FootballMatchState(
            match_id=odds_id,
            home_team=home_team,
            away_team=away_team,
            tournament=tournament,
            league_key=league_key,
            minute=0,
            home_score=0,
            away_score=0,
            home_odds=home_odds,
            draw_odds=draw_odds,
            away_odds=away_odds,
            is_scheduled=is_upcoming,
            kickoff_time=ct if is_upcoming else None,
            timestamp=datetime.now(UTC),
        )
        await self.store.update(new_state)
        log.info("football_odds_api_match_added",
                 match_id=odds_id,
                 teams=f"{home_team} vs {away_team}",
                 tournament=tournament,
                 scheduled=is_upcoming,
                 home_odds=home_odds,
                 draw_odds=draw_odds,
                 away_odds=away_odds)
        return odds_id


def _best_price_by_index(bookmakers: list[dict], idx: int) -> float:
    best = 0.0
    for bm in bookmakers:
        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            if len(outcomes) > idx:
                price = float(outcomes[idx].get("price", 0) or 0)
                if price > 1.0 and (best == 0.0 or price < best):
                    best = price
    return best
