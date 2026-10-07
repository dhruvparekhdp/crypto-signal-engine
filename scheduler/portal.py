"""
The portal: four hubs (Command, Book, Evidence, System) served from one page shell.

Design, palettes, page map, redirects and the micro UI model: docs/PORTAL_REVAMP.md. The front end
is plain HTML/CSS/JS in scheduler/portal/ with no build step:

  nav.js          the one navigation map (hubs → pages), used by the shell and by every tool page
  core.js         store (one request per endpoint, shared), component kit, widget runtime, shell
  hubs/<hub>.js   that hub's widgets and views, loaded on the first visit to the hub
  chrome.js/.css  the same header + the "brutal" skin for the server-rendered tool pages

Routes
  /                          -> 302 /command
  /<hub>, /<hub>/<page>      the shell; the page picks the view from the path
  /portal/static/<asset>     fixed whitelist; ?v=<content hash> is cached for a year, anything else revalidates
  /data                      -> 301 /system/data (the rest of the retired addresses are forwarded in chrome.js)
  /api/portal/candles        closed bars for one coin (Binance futures klines, cached 60 s)
  /api/portal/signals        recent swing-book signals with their market-filter verdict
  /api/portal/meta           the few settings the page needs to state what it is (paper/live, filter)
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web

STATIC = Path(__file__).with_name("portal")
HUBS = ("command", "book", "evidence", "system")
ASSETS = {
    "portal.css": "text/css",
    "chrome.css": "text/css",
    "nav.js": "application/javascript",
    "core.js": "application/javascript",
    "chrome.js": "application/javascript",
    "hubs/command.js": "application/javascript",
    "hubs/book.js": "application/javascript",
    "hubs/evidence.js": "application/javascript",
    "hubs/system.js": "application/javascript",
}
REDIRECTS = {"/data": "/system/data"}
TIMEFRAMES = ("1h", "4h", "8h", "1d")
CANDLE_TTL_S = 60
_PAGE_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_candles: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_assets: dict[str, tuple[str, str]] = {}       # name -> (text, hash)
_shell: str | None = None


def _json(data, status: int = 200) -> web.Response:
    return web.Response(text=json.dumps(data, default=str), status=status, content_type="application/json")


def _int(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(raw))) if raw is not None else default
    except ValueError:
        return default


def _asset(name: str) -> tuple[str, str]:
    """Text and content hash of one whitelisted asset, read once per process (files ship with the deploy)."""
    if name not in _assets:
        text = (STATIC / name).read_text(encoding="utf-8")
        _assets[name] = (text, hashlib.sha256(text.encode()).hexdigest()[:10])
    return _assets[name]


def asset_url(name: str) -> str:
    return f"/portal/static/{name}?v={_asset(name)[1]}"


def chrome_snippet() -> str:
    """Head tags that give a server-rendered page the portal header and skin. Not deferred on
    purpose: chrome.js forwards retired addresses before the old page starts loading its data."""
    return (f'<link rel="stylesheet" href="{asset_url("chrome.css")}">'
            f'<script src="{asset_url("nav.js")}"></script>'
            f'<script src="{asset_url("chrome.js")}"></script>')


def _render_shell() -> str:
    global _shell
    if _shell is None:
        html = (STATIC / "portal.html").read_text(encoding="utf-8")
        html = re.sub(r"\{\{asset:([\w./-]+)\}\}", lambda m: asset_url(m.group(1)), html)
        hubs = {n: asset_url(n) for n in ASSETS if n.startswith("hubs/")}
        _shell = html.replace("{{assets_json}}", json.dumps(hubs))
    return _shell


async def root_redirect(request: web.Request) -> web.Response:
    raise web.HTTPFound("/command")


async def retired(request: web.Request) -> web.Response:
    raise web.HTTPMovedPermanently(REDIRECTS[request.path])


async def portal_page(request: web.Request) -> web.Response:
    page = request.match_info.get("page")
    if page is not None and not _PAGE_RE.match(page):
        raise web.HTTPNotFound()
    return web.Response(text=_render_shell(), content_type="text/html")


async def portal_asset(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if name not in ASSETS:                       # a fixed list: no path ever reaches the filesystem unchecked
        raise web.HTTPNotFound()
    text, digest = _asset(name)
    cache = "public, max-age=31536000, immutable" if request.query.get("v") == digest else "no-cache"
    return web.Response(text=text, content_type=ASSETS[name], headers={"Cache-Control": cache, "ETag": f'"{digest}"'})


async def api_candles(request: web.Request) -> web.Response:
    """Closed bars for the Command chart, from the same klines call the swing scan uses."""
    sym = request.query.get("symbol", "BTCUSDT").upper()
    tf = request.query.get("tf", "4h").lower()
    if not sym.isalnum() or len(sym) > 20:
        return _json({"error": "bad symbol"}, 400)
    if tf not in TIMEFRAMES:
        return _json({"error": f"tf must be one of {', '.join(TIMEFRAMES)}"}, 400)
    limit = _int(request.query.get("limit"), 60, 10, 200)
    now = time.time()
    hit = _candles.get((sym, tf))
    if hit is None or now - hit[0] > CANDLE_TTL_S:
        import httpx

        from analysis import swing_book as sb
        try:
            async with httpx.AsyncClient() as client:
                b = await sb.fetch_bars(client, sym, int(now * 1000), tf, limit=200)
        except Exception as e:  # noqa: BLE001 - an exchange hiccup is a 502 for this panel, not a crash
            return _json({"error": "candles unavailable", "detail": str(e)[:160]}, 502)
        bars = [] if b is None else [
            {"t": int(t), "o": float(o), "h": float(h), "l": float(lo), "c": float(c)}
            for t, o, h, lo, c in zip(b.t, b.o, b.h, b.l, b.c)]
        hit = (now, bars)
        _candles[(sym, tf)] = hit
    return _json(hit[1][-limit:])


async def api_signals(request: web.Request) -> web.Response:
    """Swing-book signals, newest first, each with the market-filter verdict it was tagged with."""
    from analysis import regime_gate
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    days = _int(request.query.get("days"), 14, 1, 120)
    limit = _int(request.query.get("limit"), 40, 1, 200)
    try:
        async with AsyncSessionFactory() as session:
            rows = await Repository(session).swing_signals_since(days, limit=limit)
    except Exception as e:  # noqa: BLE001 - the page shows the error instead of an empty feed
        return _json({"signals": [], "error": str(e)[:200]})
    from scheduler.pipeline import REASON_WORDS

    out = []
    for r in rows:
        tag = regime_gate.parse_tag(r.indicators_summary or "") or {}
        ts = r.timestamp if r.timestamp.tzinfo else r.timestamp.replace(tzinfo=UTC)
        reason = r.skip_reason or ""
        # Same rule as the classic Signals tab: no skip reason means it became a paper trade.
        status = "TRADED" if not reason else ("FILTER BLOCK" if tag.get("would_skip") else "NOT TRADED")
        out.append({
            "status": status,
            "skip_reason_text": REASON_WORDS.get(reason, reason.replace("_", " ")) if reason else "",
            "id": r.id, "time": ts.isoformat(), "symbol": r.symbol.upper(), "strategy": r.signal_type,
            "direction": r.direction, "timeframe": r.timeframe, "price": r.current_price,
            "stop": r.stop_loss, "target": r.target_price, "outcome": r.outcome, "pnl_pct": r.pnl_pct,
            "skip_reason": reason, "btc_vol_rank": tag.get("btc_vol_rank"),
            "filter_skip": bool(tag.get("would_skip")), "filter_reasons": tag.get("reasons", []),
        })
    return _json({"signals": out})


async def api_meta(request: web.Request) -> web.Response:
    from config.settings import settings
    return _json({
        "live_trading_mode": settings.live_trading_mode,
        "paper_trading_enabled": settings.paper_trading_enabled,
        "swing_enabled": settings.swing_enabled,
        "regime_filter": settings.swing_regime_filter,
        "vol_rank_max": settings.swing_regime_vol_rank_max,
        "server_time": datetime.now(UTC).isoformat(),
    })


def register(app: web.Application) -> None:
    app.router.add_get("/", root_redirect)
    for hub in HUBS:
        app.router.add_get(f"/{hub}", portal_page)
        app.router.add_get(f"/{hub}/{{page}}", portal_page)
    for old in REDIRECTS:
        app.router.add_get(old, retired)
    app.router.add_get("/portal/static/{name:.+}", portal_asset)
    app.router.add_get("/api/portal/candles", api_candles)
    app.router.add_get("/api/portal/signals", api_signals)
    app.router.add_get("/api/portal/meta", api_meta)
