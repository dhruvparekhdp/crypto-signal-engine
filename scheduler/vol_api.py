"""
Delta-hedged straddle paper research, for the portal (analysis/vol_book.py; owner 22 Oct 2026).

    GET /api/vol     open positions (marked at Delta's option mark price), results by book, recent closed
"""
from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

_OPEN_KEYS = ("key", "underlying", "tenor", "side", "expiry", "strike", "opened", "entry_price", "entry_premium",
              "entry_iv", "iv_now", "realized_vol", "hedges", "delta_now", "hedge", "opt_pnl", "hedge_pnl", "fees",
              "total_usd", "total_pct", "notional")


def register(app: web.Application, runner) -> None:
    async def vol(request: web.Request) -> web.Response:
        from analysis import vol_book as vb
        from config.settings import settings
        closed = []
        try:
            for ln in Path(getattr(runner, "_VOL_CLOSED", "data/vol_trades.jsonl")).read_text().splitlines():
                try:
                    closed.append(json.loads(ln))
                except ValueError:
                    continue
        except OSError:
            pass
        try:
            book = json.loads(Path(getattr(runner, "_VOL_OPEN", "data/vol_book.json")).read_text())
        except (OSError, ValueError):
            book = {"open": {}}
        return web.json_response({
            "plan": {"notional_usd": settings.vol_notional_usd, "hedge_band": settings.vol_hedge_band,
                     "close_before_min": vb.CLOSE_BEFORE.total_seconds() / 60, "max_spread_pct": vb.MAX_SPREAD * 100,
                     "option_fee_pct": vb.OPT_FEE * 100, "perp_fee_pct": vb.PERP_FEE * 100},
            "started": book.get("started"), "checked": book.get("checked"),
            "open": [{k: p.get(k) for k in _OPEN_KEYS} for p in sorted(book.get("open", {}).values(),
                                                                         key=lambda p: p["key"])],
            "summary": vb.summary(closed),
            "recent": [{k: r.get(k) for k in _OPEN_KEYS + ("closed", "exit", "move_pct", "spread_paid")}
                       for r in closed[-30:]][::-1]})

    app.router.add_get("/api/vol", vol)
