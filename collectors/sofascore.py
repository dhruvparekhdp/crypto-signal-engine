"""
Sofascore unofficial API collector.

Polls the Sofascore internal JSON API for live tennis matches every N seconds.
Uses full browser-grade headers to avoid data-center IP blocks.
Falls back gracefully when Sofascore returns 403/429.
"""
import asyncio
import random
from datetime import UTC, datetime, timedelta

import httpx
import structlog

from analysis.match_state import MatchState, OddsPoint, ServeStats
from analysis.state_store import MatchStateStore
from collectors.base import BaseCollector

log = structlog.get_logger()

_BASE = "https://api.sofascore.com/api/v1"

# Full browser headers — essential for avoiding Cloudflare/WAF blocks on cloud IPs
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Connection": "keep-alive",
}

_SURFACE_MAP = {
    "Hard": "hard",
    "Clay": "clay",
    "Grass": "grass",
    "Indoor Hard": "indoor_hard",
    "Carpet": "indoor_hard",
}

# Consecutive failure counter — if Sofascore is consistently blocked we back off
_MAX_CONSECUTIVE_FAILURES = 5
_BACKOFF_SECONDS = [10, 30, 60, 120, 300]  # escalating wait after each failure


class SofascoreCollector(BaseCollector):
    def __init__(self, store: MatchStateStore) -> None:
        self.store = store
        self._client: httpx.AsyncClient | None = None
        self._consecutive_failures = 0
        self._blocked_until: datetime | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=_HEADERS,
                timeout=20.0,
                follow_redirects=True,
                http2=True,        # Sofascore prefers HTTP/2
            )
        return self._client

    async def fetch(self) -> None:
        """Fetch all live tennis events and update the state store."""
        # Respect back-off window
        if self._blocked_until and datetime.now(UTC) < self._blocked_until:
            remaining = int((self._blocked_until - datetime.now(UTC)).total_seconds())
            log.debug("sofascore_backoff_active", wait_secs=remaining)
            return

        try:
            events = await self._get_live_events()
            self._consecutive_failures = 0   # reset on success
            self._blocked_until = None
        except httpx.HTTPStatusError as exc:
            self._on_failure(exc.response.status_code)
            return
        except Exception as exc:
            self._on_failure(error=str(exc))
            return

        for event in events:
            try:
                state = await self._build_match_state(event)
                if state:
                    await self.store.update(state)
            except Exception:
                log.exception("sofascore_build_state_failed", event_id=event.get("id"))

        # Remove matches that are no longer live
        current_ids = {str(e["id"]) for e in events}
        for state in await self.store.get_all():
            if state.match_id not in current_ids:
                await self.store.remove(state.match_id)

        if events:
            log.info("sofascore_fetched", live_matches=len(events))

    def _on_failure(self, status_code: int | None = None, error: str | None = None) -> None:
        self._consecutive_failures += 1
        wait = _BACKOFF_SECONDS[min(self._consecutive_failures - 1, len(_BACKOFF_SECONDS) - 1)]
        self._blocked_until = datetime.now(UTC) + timedelta(seconds=wait)

        if status_code == 403:
            log.warning(
                "sofascore_blocked_403",
                hint="Cloud IP blocked by WAF — retrying with backoff",
                backoff_secs=wait,
                consecutive_failures=self._consecutive_failures,
            )
        elif status_code == 429:
            log.warning(
                "sofascore_rate_limited_429",
                backoff_secs=wait,
            )
        else:
            log.error(
                "sofascore_fetch_failed",
                status_code=status_code,
                error=error,
                backoff_secs=wait,
            )

    async def _get_live_events(self) -> list[dict]:
        client = await self._get_client()
        # Small random jitter to avoid fingerprinting by request timing
        await asyncio.sleep(random.uniform(0.5, 2.0))
        resp = await client.get(f"{_BASE}/sport/tennis/events/live")
        resp.raise_for_status()
        return resp.json().get("events", [])

    async def _build_match_state(self, event: dict) -> MatchState | None:
        match_id = str(event.get("id", ""))
        if not match_id:
            return None

        status = event.get("status", {}).get("type", "")
        if status != "inprogress":
            return None

        home = event.get("homeTeam", {}).get("name", "Unknown")
        away = event.get("awayTeam", {}).get("name", "Unknown")
        tournament = (
            event.get("tournament", {}).get("name", "")
            or event.get("tournament", {}).get("uniqueTournament", {}).get("name", "Unknown")
        )
        ground = event.get("groundType") or event.get("tournament", {}).get("groundType", "Hard")
        surface = _SURFACE_MAP.get(ground, "hard")

        home_score = event.get("homeScore", {})
        away_score = event.get("awayScore", {})
        sets_p1 = home_score.get("current", 0)
        sets_p2 = away_score.get("current", 0)

        current_set = sets_p1 + sets_p2 + 1
        games_p1 = home_score.get(f"period{current_set}", 0)
        games_p2 = away_score.get(f"period{current_set}", 0)

        serving = event.get("serving", 0)
        current_server = 1 if serving == 1 else (2 if serving == 2 else 0)

        stats = await self._get_match_stats(match_id)
        serve_stats_p1, serve_stats_p2 = self._parse_serve_stats(stats)
        odds_p1, odds_p2 = await self._get_odds(match_id)

        existing = await self.store.get(match_id)
        odds_history: list[OddsPoint] = []
        game_log: list[int] = []
        match_duration = event.get("time", {}).get("played", 0) // 60

        if existing:
            odds_history = existing.odds_history.copy()
            game_log = existing.game_log.copy()
            prev_total = existing.games_in_set_p1 + existing.games_in_set_p2
            curr_total = games_p1 + games_p2
            if curr_total > prev_total:
                if games_p1 > existing.games_in_set_p1:
                    game_log.append(1)
                elif games_p2 > existing.games_in_set_p2:
                    game_log.append(2)

        if odds_p1 > 1.0 and odds_p2 > 1.0:
            odds_history.append(OddsPoint(odds_p1=odds_p1, odds_p2=odds_p2, timestamp=datetime.now(UTC)))
            if len(odds_history) > 40:
                odds_history = odds_history[-40:]

        return MatchState(
            match_id=match_id,
            player1_name=home,
            player2_name=away,
            surface=surface,
            tournament=tournament,
            current_server=current_server,
            sets_p1=sets_p1,
            sets_p2=sets_p2,
            games_in_set_p1=games_p1,
            games_in_set_p2=games_p2,
            current_set=current_set,
            is_tiebreak=games_p1 >= 6 and games_p2 >= 6 and abs(games_p1 - games_p2) < 2,
            serve_stats_p1=serve_stats_p1,
            serve_stats_p2=serve_stats_p2,
            odds_p1=odds_p1,
            odds_p2=odds_p2,
            odds_history=odds_history,
            game_log=game_log,
            match_duration_mins=match_duration,
            timestamp=datetime.now(UTC),
        )

    async def _get_match_stats(self, match_id: str) -> dict:
        try:
            client = await self._get_client()
            await asyncio.sleep(random.uniform(0.2, 0.8))
            resp = await client.get(f"{_BASE}/event/{match_id}/statistics")
            if resp.status_code == 200:
                return resp.json()
            log.debug("sofascore_stats_non_200", status=resp.status_code, match_id=match_id)
        except Exception:
            log.debug("sofascore_stats_fetch_failed", match_id=match_id)
        return {}

    async def _get_odds(self, match_id: str) -> tuple[float, float]:
        try:
            client = await self._get_client()
            resp = await client.get(f"{_BASE}/event/{match_id}/odds/1/all")
            if resp.status_code != 200:
                return 0.0, 0.0
            markets = resp.json().get("markets", [])
            for market in markets:
                if "winner" in market.get("marketName", "").lower():
                    choices = market.get("choices", [])
                    if len(choices) >= 2:
                        o1 = float(choices[0].get("fractionalValue") or choices[0].get("decimalValue") or 0)
                        o2 = float(choices[1].get("fractionalValue") or choices[1].get("decimalValue") or 0)
                        return o1, o2
        except Exception:
            log.debug("sofascore_odds_fetch_failed", match_id=match_id)
        return 0.0, 0.0

    def _parse_serve_stats(self, data: dict) -> tuple[ServeStats, ServeStats]:
        s1, s2 = ServeStats(), ServeStats()
        try:
            groups = data.get("statistics", [{}])[0].get("groups", [])
            for group in groups:
                if "serve" not in group.get("groupName", "").lower():
                    continue
                for item in group.get("statisticsItems", []):
                    key = item.get("name", "").lower()
                    h, a = item.get("home", "0"), item.get("away", "0")
                    if "1st serve" in key and "%" in key:
                        s1.first_serve_pct = _pct(h)
                        s2.first_serve_pct = _pct(a)
                    elif "aces" in key:
                        s1.aces = _int(h)
                        s2.aces = _int(a)
                    elif "double fault" in key:
                        s1.double_faults = _int(h)
                        s2.double_faults = _int(a)
        except Exception:
            pass
        return s1, s2

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


def _pct(val: str) -> float:
    try:
        return float(str(val).replace("%", "").strip()) / 100
    except (ValueError, TypeError):
        return 0.60


def _int(val: str) -> int:
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return 0
