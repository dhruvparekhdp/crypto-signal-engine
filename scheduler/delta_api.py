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
            cycle = await repo.get_running_cycle()
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

    async def demo(request: web.Request) -> web.Response:
        """Demo (testnet) positions next to the REAL market: the testnet's own price drifts (15 Oct: SOL 109.15 on
        the demo vs 103.94 real), so a demo P&L must never be read as a real one."""
        import httpx

        from config.settings import settings
        from execution.delta_india import INDIA_URL, USER_AGENT
        state = _read_json(Path("data/delta_testnet_state.json"))
        positions = state.get("positions") or {}
        demo_t, real_t, cv = {}, {}, {}
        if positions:
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": USER_AGENT}) as c:
                for sym in positions:
                    for base, out in ((settings.delta_india_testnet_url, demo_t), (INDIA_URL, real_t)):
                        try:
                            r = (await c.get(f"{base}/v2/tickers/{sym}")).json().get("result") or {}
                            out[sym] = float(r.get("mark_price") or 0) or None
                        except Exception:  # noqa: BLE001
                            out[sym] = None
                    try:
                        prod = (await c.get(f"{settings.delta_india_testnet_url}/v2/products/{sym}")).json().get("result") or {}
                        cv[sym] = float(prod.get("contract_value") or 1)
                    except Exception:  # noqa: BLE001
                        cv[sym] = 1.0
        return web.json_response({"mode": settings.delta_india_mode,
                                  "positions": demo_rows(positions, demo_t, real_t, cv)})

    app.router.add_get("/api/delta", status)
    app.router.add_get("/api/delta/demo", demo)
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


def demo_rows(positions: dict, demo_marks: dict, real_marks: dict, contract_values: dict) -> list[dict]:
    """One row per demo position: demo fill, demo price, real price, the gap between them, and the demo P&L."""
    rows = []
    for sym, p in positions.items():
        side = 1 if p.get("side") == "buy" else -1
        entry = float(p.get("entry_fill") or p.get("entry") or 0)
        dm, rm = demo_marks.get(sym), real_marks.get(sym)
        size, val = int(p.get("size") or 0), float(contract_values.get(sym) or 1)
        gap = (dm - rm) / rm * 100 if dm and rm else None
        rows.append({
            "symbol": sym, "binance_symbol": p.get("binance_symbol"), "side": "long" if side > 0 else "short",
            "contracts": size, "entry": entry, "demo_mark": dm, "real_mark": rm,
            "gap_pct": round(gap, 2) if gap is not None else None,
            "demo_pnl_usd": round(side * (dm - entry) * size * val, 2) if dm and entry else None,
            "exchange_stop": p.get("exchange_stop"), "target": p.get("target"),
            "note": ("demo market is " + (f"{gap:+.1f}%" if gap is not None else "?") +
                     " vs the real price: demo P&L tests the order flow, not the strategy"),
        })
    return rows
