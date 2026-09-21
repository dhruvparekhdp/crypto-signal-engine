"""
Parimatch (pari-betting.com) live tennis collector — LAPTOP ONLY.

Parimatch blocks datacenter/cloud IPs (the Render server gets HTTP 403), so this
must run on your laptop (residential IP) where the site loads in your browser.
It drives a real Chromium via Playwright and captures the JSON/WebSocket feed the
site itself uses — no fragile HTML scraping, no ToS-violating API reverse-engineering
of private endpoints (we read what the page already fetches for you).

Two modes
─────────
1. DISCOVERY (run this first):
       python -m collectors.parimatch --discover
   Opens the live tennis page, captures every JSON HTTP response and WebSocket
   frame into ./parimatch_capture/, and prints a summary. Send me the largest
   capture file and I'll finalise the exact parser in `parse_feed()`.

2. COLLECT (after the parser is finalised):
       python -m collectors.parimatch --once       # one scrape, prints states
   Or drive it from push_client.py with --parimatch.

Install (laptop):
       pip install playwright
       playwright install chromium
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import UTC, datetime

import structlog

from analysis.match_state import MatchState
from analysis.state_store import MatchStateStore

log = structlog.get_logger()

LIVE_URL = "https://pari-betting.com/en/tennis/live"
CAPTURE_DIR = "parimatch_capture"

_SURFACE_HINTS = {
    "clay": "clay", "grass": "grass", "hard": "hard",
    "indoor": "indoor_hard", "carpet": "indoor_hard",
}


def _infer_surface(text: str) -> str:
    t = (text or "").lower()
    for hint, surf in _SURFACE_HINTS.items():
        if hint in t:
            return surf
    return "hard"


# ── Discovery ──────────────────────────────────────────────────────────────────

async def discover(headless: bool = False, wait_secs: int = 25) -> None:
    """Open the live page and dump all JSON HTTP + WebSocket payloads for analysis."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: Playwright not installed. Run:\n"
              "  pip install playwright\n  playwright install chromium")
        return

    os.makedirs(CAPTURE_DIR, exist_ok=True)
    captured_http = 0
    captured_ws = 0
    ws_frames: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            locale="en-US",
        )
        page = await context.new_page()

        async def on_response(resp):
            nonlocal captured_http
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            try:
                body = await resp.body()
            except Exception:
                return
            if len(body) < 80:
                return
            captured_http += 1
            safe = re.sub(r"[^a-zA-Z0-9]+", "_", resp.url)[-80:]
            fname = os.path.join(CAPTURE_DIR, f"http_{captured_http:03d}_{safe}.json")
            try:
                with open(fname, "wb") as f:
                    f.write(body)
                log.info("captured_http", n=captured_http, bytes=len(body), url=resp.url[:90])
            except Exception:
                pass

        def on_websocket(ws):
            log.info("websocket_opened", url=ws.url[:90])

            def on_frame(payload):
                nonlocal captured_ws
                if isinstance(payload, (bytes, bytearray)):
                    return
                if len(payload) < 40:
                    return
                captured_ws += 1
                ws_frames.append(payload)

            ws.on("framereceived", on_frame)

        page.on("response", on_response)
        page.on("websocket", on_websocket)

        print(f"Opening {LIVE_URL} … (browser window will open)")
        try:
            await page.goto(LIVE_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"Navigation warning: {exc}")
        print(f"Waiting {wait_secs}s for the live feed to load — let the page render…")
        await asyncio.sleep(wait_secs)

        # Save rendered HTML too, as a fallback for DOM parsing
        try:
            html = await page.content()
            with open(os.path.join(CAPTURE_DIR, "page.html"), "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

        # Save body innerText (human-readable structure)
        try:
            text = await page.inner_text("body")
            with open(os.path.join(CAPTURE_DIR, "body_text.txt"), "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

        if ws_frames:
            ws_path = os.path.join(CAPTURE_DIR, "websocket_frames.txt")
            with open(ws_path, "w", encoding="utf-8") as f:
                f.write("\n\n---FRAME---\n\n".join(ws_frames))

        await browser.close()

    print(f"\nDone. Captured {captured_http} JSON HTTP responses and "
          f"{captured_ws} WebSocket frames into ./{CAPTURE_DIR}/")
    print("Send me the largest http_*.json (or websocket_frames.txt) and I'll write the parser.")


# ── Parser (finalised once we have a capture) ───────────────────────────────────

def parse_feed(data: dict) -> list[MatchState]:
    """
    Convert a captured Parimatch JSON feed into MatchState objects.

    PLACEHOLDER: the exact field names depend on the captured payload. After you run
    `--discover` and share a capture, this gets filled with the precise mapping.
    Below is a best-effort generic extractor that looks for common shapes; it may
    pick up matches on its own but treat its output as provisional.
    """
    states: list[MatchState] = []
    events = _find_events(data)
    for ev in events:
        st = _try_build_state(ev)
        if st:
            states.append(st)
    return states


def _find_events(data, depth: int = 0) -> list[dict]:
    """Recursively hunt for a list of match-like dicts (two competitors + odds)."""
    found: list[dict] = []
    if depth > 6:
        return found
    if isinstance(data, list):
        # A list of dicts that each look like an event?
        dicts = [x for x in data if isinstance(x, dict)]
        if dicts and all(_looks_like_event(x) for x in dicts[:3]):
            return dicts
        for x in data:
            found.extend(_find_events(x, depth + 1))
    elif isinstance(data, dict):
        for v in data.values():
            found.extend(_find_events(v, depth + 1))
    return found


_NAME_KEYS = ("name", "title", "competitor", "team", "player")
_ODDS_KEYS = ("odds", "price", "coefficient", "coef", "rate", "value")


def _looks_like_event(d: dict) -> bool:
    keys = {k.lower() for k in d.keys()}
    has_competitors = any("competitor" in k or "team" in k or "participant" in k or "player" in k
                          for k in keys)
    has_sport = any("sport" in k or "tennis" in str(d).lower()[:200] for k in keys)
    return has_competitors or (has_sport and any("market" in k or "odds" in k for k in keys))


def _try_build_state(ev: dict) -> MatchState | None:
    """Best-effort extraction — provisional until parser is finalised from a capture."""
    try:
        comps = (ev.get("competitors") or ev.get("participants")
                 or ev.get("teams") or [])
        if isinstance(comps, dict):
            comps = list(comps.values())
        if not comps or len(comps) < 2:
            return None
        p1 = _extract_name(comps[0])
        p2 = _extract_name(comps[1])
        if not p1 or not p2 or "/" in p1 or "/" in p2:  # skip doubles / unknowns
            return None
        ev_id = str(ev.get("id") or ev.get("eventId") or ev.get("matchId") or f"{p1}_{p2}")
        match_id = f"pm_{re.sub(r'[^a-zA-Z0-9]+', '_', ev_id)}"
        tournament = (ev.get("tournament") or ev.get("league") or ev.get("competition")
                      or ev.get("category") or "Parimatch")
        if isinstance(tournament, dict):
            tournament = tournament.get("name", "Parimatch")
        return MatchState(
            match_id=match_id,
            player1_name=str(p1),
            player2_name=str(p2),
            surface=_infer_surface(str(tournament)),
            tournament=str(tournament),
            current_server=0,
            timestamp=datetime.now(UTC),
        )
    except Exception:
        return None


def _extract_name(comp) -> str:
    if isinstance(comp, str):
        return comp
    if isinstance(comp, dict):
        for k in _NAME_KEYS:
            for kk in comp:
                if kk.lower() == k or k in kk.lower():
                    v = comp[kk]
                    if isinstance(v, str) and v:
                        return v
    return ""


# ── Collect (Playwright, parses captured feed) ──────────────────────────────────

class ParimatchCollector:
    """Drives Chromium on the laptop, captures the feed, parses into MatchState."""

    def __init__(self, store: MatchStateStore | None = None, headless: bool = True) -> None:
        self.store = store
        self.headless = headless
        self._consecutive_failures = 0

    async def fetch_states(self, wait_secs: int = 18) -> list[MatchState]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            log.error("playwright_not_installed",
                      hint="pip install playwright && playwright install chromium")
            return []

        feeds: list[dict] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
                locale="en-US",
            )
            page = await context.new_page()

            async def on_response(resp):
                if "json" not in resp.headers.get("content-type", ""):
                    return
                try:
                    body = await resp.body()
                    if len(body) > 200:
                        feeds.append(json.loads(body))
                except Exception:
                    pass

            page.on("response", on_response)
            try:
                await page.goto(LIVE_URL, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(wait_secs)
            except Exception as exc:
                log.warning("parimatch_nav_failed", error=str(exc))
            await browser.close()

        states: list[MatchState] = []
        seen: set[str] = set()
        for feed in feeds:
            for st in parse_feed(feed):
                if st.match_id not in seen:
                    seen.add(st.match_id)
                    states.append(st)

        if not states:
            self._consecutive_failures += 1
            log.warning("parimatch_no_states", feeds_captured=len(feeds),
                        consecutive_failures=self._consecutive_failures)
        else:
            self._consecutive_failures = 0
            log.info("parimatch_states", count=len(states))

        if self.store is not None:
            live_ids = set()
            for st in states:
                await self.store.update(st)
                live_ids.add(st.match_id)
            for s in await self.store.get_all():
                if s.match_id.startswith("pm_") and s.match_id not in live_ids:
                    await self.store.remove(s.match_id)

        return states


# ── CLI ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Parimatch live tennis scraper (laptop).")
    parser.add_argument("--discover", action="store_true",
                        help="Capture the feed for parser development (run this first)")
    parser.add_argument("--once", action="store_true",
                        help="Run one scrape and print extracted match states")
    parser.add_argument("--headless", action="store_true",
                        help="Run browser headless (default headful for discovery)")
    args = parser.parse_args()

    if args.discover:
        asyncio.run(discover(headless=args.headless))
    elif args.once:
        async def _run():
            c = ParimatchCollector(headless=args.headless)
            states = await c.fetch_states()
            for s in states:
                print(f"  {s.player1_name} vs {s.player2_name} — {s.tournament} "
                      f"[{s.odds_p1}/{s.odds_p2}] {s.sets_p1}-{s.sets_p2} "
                      f"({s.games_in_set_p1}-{s.games_in_set_p2})")
            print(f"\n{len(states)} matches extracted.")
        asyncio.run(_run())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
