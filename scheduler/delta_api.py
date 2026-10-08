"""
Delta India status + shadow-now for open paper swing trades.

    GET  /api/delta              mode, key-set?, last shadow rows, open delta state
    POST /api/delta/shadow-now   re-run shadow mirror for every open paper swing position
                                 (does NOT place live orders even if mode=live — shadow only)
"""
from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from execution.delta_book import SHADOW_PATH, STATE_PATH


def register(app: web.Application, runner) -> None:

    async def status(request: web.Request) -> web.Response:
        from config.settings import settings
        key_set = bool(settings.delta_india_api_key and settings.delta_india_api_secret)
        shadows = _tail_jsonl(SHADOW_PATH, 20)
        state = _read_json(STATE_PATH)
        return web.json_response({
            "mode": settings.delta_india_mode,
            "live_orders": bool(settings.delta_india_live_orders),
            "key_set": key_set,
            "base_url": settings.delta_india_base_url,
            "risk_pct": settings.delta_india_risk_pct,
            "max_open": settings.delta_india_max_open,
            "usd_inr": settings.delta_india_usd_inr,
            "shadow_tail": shadows,
            "state": state,
            "note": ("shadow = log intents from paper signals, no orders. "
                     "live + live_orders=true required for real fills."),
        })

    async def shadow_now(request: web.Request) -> web.Response:
        """Shadow-mirror every open paper swing position right now (never places orders)."""
        from config.settings import settings
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository

        if not (settings.delta_india_api_key and settings.delta_india_api_secret):
            return web.json_response(
                {"ok": False, "error": "delta_india key/secret not set — add on /keys"},
                status=400)
        results = []
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            cycle = await repo.get_active_cycle()
            if cycle is None:
                return web.json_response(
                    {"ok": False, "error": "no active paper cycle"}, status=400)
            rows = await repo.get_open_positions(cycle.id)
            swings = [r for r in rows if getattr(r, "trade_mode", "") == "swing"]
            pcfg = await repo.get_paper_config()
            usdt_inr = float(
                getattr(pcfg, "usdt_inr", None) or settings.delta_india_usd_inr or 83.0)

        # Force shadow for this endpoint even if mode is live — safety
        saved_mode = settings.delta_india_mode
        saved_live = settings.delta_india_live_orders
        try:
            settings.delta_india_mode = "shadow"
            settings.delta_india_live_orders = False
            for r in swings:
                side = getattr(r.side, "value", r.side)
                try:
                    await runner._mirror_delta_india(
                        r.symbol, side, float(r.entry_price), float(r.stop_price),
                        float(r.target_price), getattr(r, "signal_type", "") or "",
                        usdt_inr=usdt_inr)
                    results.append({"symbol": r.symbol, "side": side, "ok": True})
                except Exception as e:  # noqa: BLE001
                    results.append({
                        "symbol": r.symbol, "side": side,
                        "ok": False, "error": str(e)[:160],
                    })
        finally:
            settings.delta_india_mode = saved_mode
            settings.delta_india_live_orders = saved_live

        return web.json_response({
            "ok": True, "n": len(results), "results": results,
            "shadow_file": str(SHADOW_PATH),
            "mode_restored": saved_mode,
        })

    app.router.add_get("/api/delta", status)
    app.router.add_post("/api/delta/shadow-now", shadow_now)


def _tail_jsonl(path: Path, n: int) -> list:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
