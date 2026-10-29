"""
Candidates paper track, for the portal (analysis/candidate_book.py; owner 26 Oct 2026: "only paper, no changes for live").

    GET /api/candidates     per strategy: closed results, open trades marked now, the promotion gate in plain words
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web

NAMES = {"swing": "Swing signals, live exit (every signal, no slot limit)",
         "swing_ratchet": "Swing signals, ratchet exit",
         "swing_mirror": "Swing mirrors (control)",
         "swing_2h": "2h swing (paper candidate)",
         "swing_2h_mirror": "2h swing mirrors (control)",
         "impulse_follow": "Live moves, follow the move", "impulse_fade": "Live moves, against the move (control)",
         "impulse_ai": "Live moves, the AI's call with live news",
         "intraday_pick": "Intraday picks", "intraday_mirror": "Intraday mirrors (control)",
         "counter_trend": "Counter-trend shorts (held back)", "counter_trend_mirror": "Counter-trend mirrors (control)"}


def register(app: web.Application, runner) -> None:
    async def candidates(request: web.Request) -> web.Response:
        from analysis import candidate_book as cb
        closed = []
        try:
            for ln in Path(getattr(runner, "_CAND_CLOSED", "data/candidate_trades.jsonl")).read_text().splitlines():
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if r.get("exit") != "no data" and "r_net" in r:
                    closed.append(r)
        except OSError:
            pass
        try:
            book = json.loads(Path(getattr(runner, "_CAND_OPEN", "data/candidate_book.json")).read_text())
        except (OSError, ValueError):
            book = {"open": []}
        prices = {}
        store = getattr(runner, "crypto_store", None)
        if store is not None:
            prices = {st.symbol.upper(): st.current_price for st in await store.get_all()}
        marked = [m for m in (cb.mark(t, prices.get(t["symbol"])) for t in book.get("open", [])) if m]
        card = cb.scorecard(closed, marked, cb.btc_week_dir(book.get("btc_days") or {}), book.get("started"),
                            datetime.now(UTC))
        for k, v in card["tracks"].items():
            v["name"] = NAMES.get(k, k)
        impulses = []
        try:
            for ln in Path(getattr(runner, "_IMPULSE_LOG", "data/impulse_events.jsonl")).read_text().splitlines()[-20:]:
                try:
                    impulses.append(json.loads(ln))
                except ValueError:
                    continue
        except OSError:
            pass
        return web.json_response({
            "impulses": impulses[::-1],
            "started": book.get("started"), "open": len(book.get("open", [])), "gate": cb.GATE, **card,
            "recent": [{k: r.get(k) for k in ("track", "symbol", "side", "signal_type", "label", "exit", "r_net",
                                              "closed_ts")} for r in closed[-30:]][::-1]})

    app.router.add_get("/api/candidates", candidates)
