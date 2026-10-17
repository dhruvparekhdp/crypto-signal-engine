"""
Live (real money) Delta India book, for the portal's Book > Live page (owner, 18 Oct 2026).

    GET /api/delta/live     admin only - the real account: wallet, today's P&L vs the loss limit, every open position
                            (entry, mark, liquidation, margin, unrealised P&L in Rs, the exchange stop and target, the
                            engine's lock), open orders and the live order journal. Cached 4 s; the page polls every 5 s.

Read-only: this endpoint never places, moves or cancels anything.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from aiohttp import web

from execution.delta_book import SHADOW_PATH, STATE_PATH

_cache: dict = {"t": 0.0, "body": None}
CACHE_S = 4.0


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def live_rows(records: dict, exchange: list[dict], orders: list[dict], rate: float, wallet_inr: float) -> list[dict]:
    """One row per position, joining our record (by contract symbol) with Delta's margined position and its open
    exit orders. A position on Delta that we have no record of is shown too (flagged), never hidden."""
    by_sym = {str(p.get("product_symbol") or "").upper(): p for p in exchange if _f(p.get("size"), 0)}
    syms = list(dict.fromkeys(list(records) + list(by_sym)))
    out = []
    for sym in syms:
        rec, ex = records.get(sym) or {}, by_sym.get(sym) or {}
        size = int(_f(ex.get("size"), 0) or 0)
        side = "short" if (size < 0 or rec.get("side") == "sell") else "long"
        entry = _f(ex.get("entry_price")) or _f(rec.get("entry_fill")) or _f(rec.get("entry"))
        mark = _f(ex.get("mark_price"))
        upnl_usd = _f(ex.get("unrealized_pnl"))
        cv = _f(ex.get("product", {}).get("contract_value") if isinstance(ex.get("product"), dict) else None) or \
            _f(rec.get("contract_value")) or 1.0
        if upnl_usd is None and entry and mark and size:
            upnl_usd = (mark - entry) * size * cv
        pid = ex.get("product_id") or rec.get("product_id")
        mine = [o for o in orders if str(o.get("product_id")) == str(pid)]
        stop_o = next((o for o in mine if o.get("stop_order_type") == "stop_loss_order"), None)
        tp_o = next((o for o in mine if o.get("stop_order_type") == "take_profit_order"), None)
        init_stop, risk = _f(rec.get("init_stop")), _f(rec.get("risk"))
        r_now = None
        if entry and mark and risk:
            r_now = ((mark - entry) if side == "long" else (entry - mark)) / risk
        upnl_inr = upnl_usd * rate if upnl_usd is not None else None
        out.append({
            "symbol": sym, "side": side, "contracts": abs(size) if size else rec.get("size"),
            "entry": entry, "mark": mark, "liquidation": _f(ex.get("liquidation_price")),
            "margin_inr": (_f(ex.get("margin"), 0) or 0) * rate,
            "upnl_inr": round(upnl_inr, 2) if upnl_inr is not None else None,
            "upnl_pct_wallet": round(upnl_inr / wallet_inr * 100, 2) if upnl_inr is not None and wallet_inr else None,
            "r_now": round(r_now, 2) if r_now is not None else None,
            "exchange_stop": _f(stop_o.get("stop_price")) if stop_o else None,
            "exchange_target": _f(tp_o.get("stop_price")) if tp_o else None,
            "engine_stop": _f(rec.get("stop")), "init_stop": init_stop, "leverage": rec.get("leverage"),
            "opened_ms": rec.get("opened_ms"), "signal_type": rec.get("signal_type"),
            "binance_symbol": rec.get("binance_symbol"),
            "flags": ([] if rec else ["on Delta but not in our records"]) + ([] if ex or not rec else
                                                                               ["in our records but flat on Delta"])
                     + ([] if stop_o or not size else ["NO STOP ON EXCHANGE"]),
        })
    return out


def _tail(path: Path, n: int) -> list[dict]:
    try:
        lines = path.read_text().splitlines()[-n:]
    except OSError:
        return []
    rows = []
    for ln in lines:
        try:
            rows.append(json.loads(ln))
        except ValueError:
            pass
    return rows


async def snapshot() -> dict:
    import httpx

    from config.settings import settings
    from execution.delta_india import USER_AGENT, DeltaIndia
    mode = (settings.delta_india_mode or "off").lower()
    body = {"mode": mode, "live_orders": bool(settings.delta_india_live_orders),
            "live": mode == "live" and bool(settings.delta_india_live_orders),
            "kill_switch": bool(settings.delta_india_kill_switch), "risk_pct": settings.delta_india_risk_pct,
            "max_open": settings.delta_india_max_open, "min_leverage": settings.delta_india_min_leverage,
            "daily_loss_pct": settings.delta_india_daily_loss_pct, "floor_inr": settings.delta_india_min_wallet_inr,
            "generated_ms": int(time.time() * 1000), "positions": [], "orders": [], "journal": [], "error": ""}
    try:
        state = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        state = {}
    records = state.get("positions") or {}
    day = state.get("day") or {}
    body["journal"] = [r for r in _tail(SHADOW_PATH, 60) if r.get("mode") == "live" or r.get("event")][-25:]
    if not (settings.delta_india_api_key and settings.delta_india_api_secret):
        body["error"] = "no live Delta key saved"
        return body
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": USER_AGENT}) as http:
            c = DeltaIndia(settings.delta_india_api_key.get_secret_value(),
                           settings.delta_india_api_secret.get_secret_value(),
                           base_url=settings.delta_india_base_url, client=http)
            avail, rate = await c.wallet_inr()
            total = await c.wallet_total_inr()
            rate = rate or float(settings.delta_india_usd_inr or 85.0)
            ex = await c._request("GET", "/v2/positions/margined")
            orders = await c._request("GET", "/v2/orders", params={"states": "open,pending"})
    except Exception as e:  # noqa: BLE001
        body["error"] = f"Delta read failed: {str(e)[:160]}"
        return body
    ex = ex if isinstance(ex, list) else []
    orders = orders if isinstance(orders, list) else []
    start = _f(day.get("start_inr")) if day.get("date") == time.strftime("%Y-%m-%d", time.gmtime()) else None
    body.update({
        "wallet": {"available_inr": round(avail, 2), "total_inr": round(total, 2), "rate": rate},
        "day": {"start_inr": start, "pnl_inr": round(total - start, 2) if start else None,
                "limit_inr": round(start * settings.delta_india_daily_loss_pct, 2) if start else None},
        "positions": live_rows(records, ex, orders, rate, total),
        "orders": [{"symbol": o.get("product_symbol"), "type": o.get("stop_order_type") or o.get("order_type"),
                    "side": o.get("side"), "size": o.get("size"), "stop_price": _f(o.get("stop_price")),
                    "limit_price": _f(o.get("limit_price")), "reduce_only": o.get("reduce_only"),
                    "state": o.get("state")} for o in orders],
    })
    return body


def register(app: web.Application, runner) -> None:
    async def live(request: web.Request) -> web.Response:
        from scheduler.settings_page import _admin
        if await _admin(request) is not None:          # real wallet and positions: admin login only
            return web.json_response({"error": "admin login needed: sign in on Settings"}, status=401)
        if _cache["body"] is None or time.time() - _cache["t"] > CACHE_S:
            _cache.update(t=time.time(), body=await snapshot())
        return web.json_response(_cache["body"])

    app.router.add_get("/api/delta/live", live)
