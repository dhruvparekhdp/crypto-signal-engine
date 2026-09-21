"""
Flashscore live feed collector — Playwright-based.

Uses a headless Chromium browser to open flashscore.com/tennis/ and intercepts
the internal /x/feed/ request the page makes. This bypasses Cloudflare, which
blocks plain httpx requests with an empty "0" response.

Feed field mapping (discovered June 2026):
  AA = match ID
  AB = status: 1=prematch, 2=live, 3=finished
  AC = sub-status: 17=set1, 18=set2+, 19=set3+, 48=tiebreak
  AE = player 1 full name (or doubles pair "P1/P2")
  AF = player 2 full name
  CX = player 1 short name
  BA/BB = set 1 scores (p1/p2), BC/BD = set 2, BE/BF = set 3, BG/BH = set 4, BI/BJ = set 5
  Tournament injected from preceding ZA header record.
"""
import asyncio
from datetime import UTC, datetime

import structlog

from analysis.match_state import MatchState, ServeStats
from analysis.state_store import MatchStateStore
from collectors.base import BaseCollector

log = structlog.get_logger()

_SET_KEYS = [("BA", "BB"), ("BC", "BD"), ("BE", "BF"), ("BG", "BH"), ("BI", "BJ")]


def _parse_record(block: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in block.split("¬"):
        if "÷" in field:
            key, _, val = field.partition("÷")
            result[key.strip()] = val.strip()
    return result


class FlashscoreCollector(BaseCollector):
    def __init__(self, store: MatchStateStore) -> None:
        self.store = store
        self._consecutive_failures = 0
        self._consecutive_zero_matches = 0

    async def fetch(self) -> None:
        if self._consecutive_failures >= 10:
            log.warning("flashscore_stopped", reason="too many consecutive failures")
            return

        raw = await self._fetch_via_playwright()
        if not raw or raw.strip() == "0":
            self._consecutive_failures += 1
            log.warning("flashscore_empty_response",
                        consecutive_failures=self._consecutive_failures)
            return

        self._consecutive_failures = 0
        await self._process(raw)

    async def _fetch_via_playwright(self) -> str:
        """
        Open FlashScore tennis page with Playwright, intercept the live-feed
        HTTP request the page makes internally. Bypasses Cloudflare.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            log.error("playwright_not_installed",
                      hint="pip install playwright && playwright install chromium")
            return ""

        all_feeds: list[tuple[int, str]] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = await context.new_page()

            async def on_response(resp) -> None:
                if "/x/feed/f_" in resp.url:
                    try:
                        body = await resp.text()
                        if body and body.strip() != "0":
                            all_feeds.append((len(body), body))
                            log.debug("flashscore_feed_intercepted",
                                      url=resp.url, len=len(body))
                    except Exception:
                        pass

            page.on("response", on_response)

            try:
                await page.goto(
                    "https://www.flashscore.com/tennis/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                for _ in range(20):
                    if all_feeds:
                        break
                    await asyncio.sleep(0.5)
            except Exception as exc:
                log.warning("flashscore_playwright_nav_failed", error=str(exc))
            finally:
                await browser.close()

        if not all_feeds:
            return ""
        all_feeds.sort(key=lambda x: x[0], reverse=True)
        return all_feeds[0][1]

    async def _process(self, raw: str) -> None:
        live_ids: set[str] = set()
        matches_found = 0

        blocks = [b for b in raw.split("~") if b.strip()]
        records = [_parse_record(b) for b in blocks]

        # Track current tournament from ZA header records
        current_tournament = "Unknown"
        for record in records:
            if not record:
                continue
            if "ZA" in record:
                current_tournament = record["ZA"]
                continue

            match_id = record.get("AA", "")
            if not match_id or record.get("AB") != "2":
                continue

            matches_found += 1
            full_id = f"fs_{match_id}"
            state = self._build_state(full_id, record, current_tournament)
            if state:
                await self.store.update(state)
                live_ids.add(full_id)

        for state in await self.store.get_all():
            if state.match_id.startswith("fs_") and state.match_id not in live_ids:
                await self.store.remove(state.match_id)

        if matches_found == 0:
            self._consecutive_zero_matches += 1
            log.warning("flashscore_zero_matches",
                        raw_len=len(raw), total_blocks=len(blocks),
                        consecutive_zeros=self._consecutive_zero_matches)
        else:
            self._consecutive_zero_matches = 0

        log.info("flashscore_collector_done", live_matches=len(live_ids),
                 total_records=matches_found)

    def _build_state(self, match_id: str, r: dict[str, str],
                     tournament: str) -> MatchState | None:
        try:
            player1 = r.get("AE", "Unknown")
            player2 = r.get("AF", "Unknown")

            # Skip doubles (names contain "/")
            if "/" in player1 or "/" in player2:
                return None

            surface = _infer_surface(tournament)

            # Derive set scores and current set from which keys are present
            sets_p1 = 0
            sets_p2 = 0
            games_p1, games_p2 = 0, 0
            current_set = 1

            for i, (hk, ak) in enumerate(_SET_KEYS, start=1):
                hv_raw = r.get(hk)
                av_raw = r.get(ak)
                if hv_raw is None:
                    # This set hasn't started — previous set is current
                    break
                hv = int(hv_raw or "0")
                av = int(av_raw or "0")
                # Check if next set exists (meaning this set is complete)
                next_hk = _SET_KEYS[i][0] if i < len(_SET_KEYS) else None
                if next_hk and r.get(next_hk) is not None:
                    # Set is complete
                    if hv > av:
                        sets_p1 += 1
                    elif av > hv:
                        sets_p2 += 1
                    current_set = i + 1
                else:
                    # This is the current set in progress
                    games_p1, games_p2 = hv, av
                    current_set = i
                    break

            is_tiebreak = (
                r.get("AC") == "48"
                or (games_p1 >= 6 and games_p2 >= 6 and abs(games_p1 - games_p2) < 2)
            )

            return MatchState(
                match_id=match_id,
                player1_name=player1,
                player2_name=player2,
                surface=surface,
                tournament=tournament,
                current_server=0,
                sets_p1=sets_p1,
                sets_p2=sets_p2,
                games_in_set_p1=games_p1,
                games_in_set_p2=games_p2,
                current_set=current_set,
                is_tiebreak=is_tiebreak,
                serve_stats_p1=ServeStats(),
                serve_stats_p2=ServeStats(),
                odds_p1=0.0,
                odds_p2=0.0,
                odds_history=[],
                game_log=[],
                match_duration_mins=0,
                timestamp=datetime.now(UTC),
            )
        except Exception as exc:
            log.debug("flashscore_parse_match_failed", match_id=match_id, error=str(exc))
            return None


def _infer_surface(tournament_name: str) -> str:
    name = tournament_name.lower()
    if "clay" in name:
        return "clay"
    if "grass" in name or "wimbledon" in name:
        return "grass"
    if "indoor" in name or "carpet" in name:
        return "indoor_hard"
    if any(city in name for city in ("roland", "paris", "madrid", "rome", "barcelona",
                                      "monte", "hamburg", "lyon", "geneva", "estoril",
                                      "bordeaux", "cordoba", "buenos", "rio", "bogota")):
        return "clay"
    return "hard"
