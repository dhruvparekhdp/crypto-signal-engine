"""
Channel-style bracket paper research, for the portal (analysis/bracket_book.py; owner 21 Oct 2026).

    GET /api/bracket     results by group (our picks, their mirrors, swing, coin), open count, recent closed trades
"""
from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web


def register(app: web.Application, runner) -> None:
    async def bracket(request: web.Request) -> web.Response:
        from analysis.bracket_book import (COST, LEVERAGE_SHOWN, MAX_HOLD, STOP, TARGETS, mark, pairs, summary,
                                           wallet_view)
        closed = []
        try:
            for ln in Path(getattr(runner, "_BRACKET_CLOSED", "data/bracket_trades.jsonl")).read_text().splitlines():
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if r.get("exit") != "no data" and "net_pct" in r:
                    closed.append(r)
        except OSError:
            pass
        try:
            book = json.loads(Path(getattr(runner, "_BRACKET_OPEN", "data/bracket_book.json")).read_text())
        except (OSError, ValueError):
            book = {"open": []}
        from config.settings import settings
        from datetime import UTC, datetime
        wallets = {}
        for key, src in (("levels", "levels"), ("intraday", "intraday"), ("swing", "swing")):
            rows = [r for r in closed + book.get("open", []) if r.get("source") == src and r.get("role") == "primary"]
            wallets[key] = wallet_view(rows, settings.bracket_wallet_inr, settings.bracket_margin_inr,
                                       settings.bracket_leverage, datetime.now(UTC))
        # open trades as if closed at the current price: most trades run up to 48 h, and the ones that close first
        # are the quick ones (a target then back to entry, or a full stop), so closed-only results lean early
        prices = {}
        store = getattr(runner, "crypto_store", None)
        if store is not None:
            prices = {st.symbol.upper(): st.current_price for st in await store.get_all()}
        both = closed + [m for m in (mark(t, prices.get(t["symbol"])) for t in book.get("open", [])) if m]
        return web.json_response({
            "wallets": wallets,
            "marked": summary(both), "pairs": pairs(both), "pairs_closed": pairs(closed),
            "plan": {"stop_pct": STOP * 100, "targets_pct": [round(t * 100, 2) for t in TARGETS],
                     "max_hold_hours": MAX_HOLD.total_seconds() / 3600, "cost_pct": COST * 100,
                     "roi_leverage": LEVERAGE_SHOWN},
            "started": book.get("started"), "open": len(book.get("open", [])),
            "summary": summary(closed),
            "recent": [{k: r.get(k) for k in ("symbol", "side", "role", "source", "signal_type", "exit", "legs",
                                              "net_pct", "roi_5x_pct", "closed_ts", "best", "worst", "labels")}
                       for r in closed[-40:]][::-1]})

    app.router.add_get("/api/bracket", bracket)
