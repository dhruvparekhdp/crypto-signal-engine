"""
Live-book simulator, for the portal (analysis/simulator/live_book.py; owner 27 Oct 2026).

    GET /api/sim/live-book?start=YYYY-MM-DD&end=YYYY-MM-DD&wallet=800&risk=3&max_open=4&round_up=10&specs=0,1&coins=ADA,XRP
    GET /api/sim/live-book/meta     what the trade set covers and when it was refreshed
Percentages are given as percent (risk=3 means 3%). max_open=0 means no limit.
"""
from __future__ import annotations

from aiohttp import web


def _num(q, key, default, lo, hi):
    try:
        v = float(q.get(key, default))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=f"{key} must be a number")
    if not lo <= v <= hi:
        raise web.HTTPBadRequest(text=f"{key} must be between {lo} and {hi}")
    return v


def _day(q, key):
    v = (q.get(key) or "").strip()
    if not v:
        return None
    import pandas as pd
    try:
        pd.Timestamp(v)
    except ValueError:
        raise web.HTTPBadRequest(text=f"{key} must be a date like 2025-01-31")
    return v[:10]


def register(app: web.Application, runner=None) -> None:
    async def sim(request: web.Request) -> web.Response:
        import asyncio

        from analysis.simulator import live_book as lb
        q = request.rel_url.query
        t = lb.load()
        if t.empty:
            return web.json_response({"error": "The trade set is not built yet (scripts/live_book_trades.py)."},
                                     status=503)
        specs = [int(x) for x in q["specs"].split(",") if x.strip().isdigit()] if q.get("specs") else None
        coins = [x.strip().upper() for x in q["coins"].split(",") if x.strip()] if q.get("coins") else None
        kw = dict(start=_day(q, "start"), end=_day(q, "end"), wallet=_num(q, "wallet", 800, 100, 10_000_000),
                  risk=_num(q, "risk", 3, 0.1, 10) / 100, max_open=int(_num(q, "max_open", 4, 0, 20)),
                  round_up=_num(q, "round_up", 10, 0, 25) / 100, specs=specs, coins=coins)
        out = await asyncio.to_thread(lb.run, t, **kw)      # a few thousand trades: well under a second
        out["data"] = lb.meta()
        return web.json_response(out)

    async def meta(request: web.Request) -> web.Response:
        from analysis.simulator import live_book as lb
        t = lb.load()
        return web.json_response({**lb.meta(), "specs": lb.SPEC_LABELS,
                                  "coins": sorted(t.coin.unique().tolist()) if not t.empty else []})

    app.router.add_get("/api/sim/live-book", sim)
    app.router.add_get("/api/sim/live-book/meta", meta)
