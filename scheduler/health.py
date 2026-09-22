"""Aiohttp web server: dashboard UI + JSON API endpoints."""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime

from aiohttp import web

from analysis.scalp_levels import ScalpConfig
from config.settings import settings as _SETTINGS

_start_time = datetime.now(UTC)

# One cost model for the page and the engine. The dashboard used to carry its
# own copy of the fee arithmetic in JavaScript, which drifted the moment the
# fees were recalibrated against the real ledger.
_SCALP = ScalpConfig()



# ── JSON API ──────────────────────────────────────────────────────────────────

async def _api_status(runner, request: web.Request) -> web.Response:
    status = runner.get_status()
    count = await runner.crypto_store.count()
    uptime = int((datetime.now(UTC) - _start_time).total_seconds())
    return web.Response(
        text=json.dumps({
            "uptime_seconds": uptime,
            "symbols_tracked": count,
            **status,
        }),
        content_type="application/json",
    )


async def _api_crypto_coins(runner, request: web.Request) -> web.Response:
    """Return live crypto watchlist market states with indicators."""
    states = await runner.crypto_store.get_all()
    coins = []
    for s in states:
        coins.append({
            "symbol": s.symbol.upper(),
            "base_asset": s.base_asset,
            "price": s.current_price,
            "change_24h_pct": round(s.price_change_24h_pct, 2),
            "volume_24h": s.volume_24h,
            "volume_ratio": round(s.volume_ratio, 2),
            # The number that decides every level. Surfacing it makes a dead
            # feed visible: 0.05% where the market really moves 0.15% is why
            # every target came out identical.
            "atr_pct": (round(s.atr_14 / s.current_price * 100, 4)
                        if s.current_price > 0 else None),
            "high_24h": s.high_24h,
            "low_24h": s.low_24h,
            "rsi_14": s.rsi_14,
            "macd_line": round(s.macd_line, 4),
            "bollinger_bandwidth": round(s.bollinger_bandwidth * 100, 2),
            "sentiment_score": s.sentiment_score,
            "timestamp": _iso(s.timestamp),
        })
    return web.Response(text=json.dumps(coins), content_type="application/json")


async def _api_crypto_signals(runner, request: web.Request) -> web.Response:
    """Return recent crypto trade signals."""
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        rows = await repo.get_recent_crypto_signals(hours=24)
    signals = [
        {
            "id": r.id,
            "symbol": r.symbol.upper(),
            "signal_type": r.signal_type,
            "direction": r.direction,
            "trigger": r.trigger_description,
            "confidence": round(r.confidence * 100),
            "current_price": r.current_price,
            "target_price": r.target_price,
            "stop_loss": r.stop_loss,
            "edge_pct": r.edge_pct,
            "stake_pct": round(r.stake_pct * 100, 2),
            "timeframe": r.timeframe,
            "sentiment_score": r.sentiment_score,
            "indicators": r.indicators_summary,
            "outcome": r.outcome,
            "timestamp": _iso(r.timestamp),
        }
        for r in rows
    ]
    return web.Response(text=json.dumps(signals), content_type="application/json")



def _iso(dt):
    """
    Serialise a timestamp so the browser cannot mistake it for local time.

    Every DateTime column here is naive UTC, and _iso(datetime) on a
    naive value emits no offset. JavaScript parses an offset-less date-time as
    LOCAL time, so a browser in IST read a UTC instant as an IST wall clock —
    signals displayed 5h30m early with an "ago" that was 5h30m too large. The
    stored instants were always correct; only the wire format was ambiguous.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _signal_row(r) -> dict:
    return {
        "id": r.id, "symbol": r.symbol.upper(), "signal_type": r.signal_type,
        "direction": r.direction, "confidence": round(r.confidence * 100),
        "current_price": r.current_price, "target_price": r.target_price,
        "stop_loss": r.stop_loss, "edge_pct": r.edge_pct,
        "timeframe": r.timeframe, "outcome": r.outcome, "pnl_pct": r.pnl_pct,
        "timestamp": _iso(r.timestamp),
    }


async def _api_predict(runner, request: web.Request) -> web.Response:
    """
    A band and a lean for every watchlist market, at three horizons.

    Separate from the signal feed on purpose. Signals answer "is there a trade
    worth its costs right now", and the honest answer is usually no — which
    leaves the board empty and says nothing about the market. This says
    something about every market, all the time.
    """
    from analysis import indicators as ind
    from analysis.forecast import detect_surge, forecast_symbol

    rows, surges = [], []
    for st in sorted(await runner.crypto_store.get_all(), key=lambda x: x.symbol):
        fc = forecast_symbol(st)
        candles = st.candles_1m or []
        closed = [c for c in candles if c.is_closed]
        rel = ind.relative_volume([c.volume for c in closed]) if closed else None
        newest = candles[-1].timestamp if candles else None
        lag = (None if newest is None else
               round((datetime.now(UTC) - (newest if newest.tzinfo else newest.replace(tzinfo=UTC))).total_seconds() / 60, 1))
        row = {
            "symbol": st.symbol.upper(),
            "price": st.current_price,
            "change_24h_pct": round(st.price_change_24h_pct, 2),
            "rsi": round(st.rsi_14, 1),
            "atr_pct": (round(st.atr_14 / st.current_price * 100, 4)
                        if st.current_price > 0 else None),
            "relative_volume": round(rel, 2) if rel is not None else None,
            "candles": len(candles),
            "data_age_minutes": lag,
            # Anything older than half an hour is not a current view of the
            # market, and a page that renders it identically to a live one is
            # how a stale board gets traded.
            "stale": lag is not None and lag > 30,
            "forecasts": [f.as_dict() for f in fc],
        }
        if fc:
            row["direction"] = fc[0].direction
            row["confidence"] = fc[0].confidence
        rows.append(row)

        surge = detect_surge(st)
        if surge is not None:
            surges.append({
                "symbol": surge.symbol.upper(), "kind": surge.kind,
                "detail": surge.detail, "magnitude": surge.magnitude,
                "direction": surge.direction,
            })

    surges.sort(key=lambda x: -x["magnitude"])
    return web.json_response({
        "generated_at": _iso(datetime.now(UTC)),
        "note": ("The band is roughly one standard deviation of this market's "
                 "own recent range projected over the horizon: price should "
                 "land inside it about two times in three. The centre is "
                 "shifted from spot by where the evidence leans, capped at a "
                 "third of the band — no indicator set earns more than that."),
        "markets": rows,
        "surges": surges,
    })


async def _api_debug_signals(runner, request: web.Request) -> web.Response:
    """
    Why each watchlist symbol did or did not produce a signal, right now.

    Written because "only XRP is firing" cannot be answered from the outside:
    every gate that refuses a setup logs at debug and then the setup vanishes.
    This asks each gate the same question the engine does and reports the
    first one that says no, per symbol.
    """
    from analysis import indicators as ind
    from analysis.confluence import ConvictionGate, evaluate
    from analysis.crypto_signals import GATE, SCALP
    from analysis.scalp_levels import NoTrade, REASON_TEXT, scalp_levels

    # The feed's own state, first. Every number below is downstream of it, and
    # a stale feed renders exactly like a live one.
    kl = getattr(runner, "klines", None)
    feed = {"source": "binance klines (REST)"}
    if kl is not None:
        age = (None if kl.last_success is None else
               round((datetime.now(UTC) - (kl.last_success if kl.last_success.tzinfo else kl.last_success.replace(tzinfo=UTC))).total_seconds() / 60, 1))
        feed.update({
            "host": kl.host or "none answered",
            "last_success_minutes_ago": age,
            "refreshed_last_sweep": sorted(kl.refreshed),
            "per_symbol": kl.status,
            "last_error": kl.last_error or None,
        })
        if age is None:
            feed["warning"] = ("this feed has never succeeded — every candle "
                               "below is preloaded archive data or poll "
                               "aggregation, not live market data")

    out = []
    for st in sorted(await runner.crypto_store.get_all(), key=lambda x: x.symbol):
        cfg = SCALP.for_symbol(st.symbol)
        row = {
            "symbol": st.symbol.upper(),
            "price": st.current_price,
            "candles": len(st.candles_1m),
            "cost_floor_pct": round(cfg.cost_floor_pct * 100, 4),
            "min_target_pct": round(cfg.min_target_pct * 100, 4),
        }

        if st.current_price <= 0:
            row["verdict"] = "no price feed"
            out.append(row)
            continue

        atr_pct = st.atr_14 / st.current_price if st.atr_14 > 0 else 0.0
        row["atr_pct"] = round(atr_pct * 100, 4)
        row["atr_vs_floor"] = round(atr_pct / cfg.cost_floor_pct, 2) if cfg.cost_floor_pct else None

        if len(st.candles_1m) < 60:
            row["verdict"] = f"warming up — {len(st.candles_1m)}/60 candles"
            out.append(row)
            continue

        highs = [c.high for c in st.candles_1m]
        lows = [c.low for c in st.candles_1m]
        closes = [c.close for c in st.candles_1m]
        flat = sum(1 for c in st.candles_1m if c.high == c.low)
        row["flat_candles"] = f"{flat}/{len(st.candles_1m)}"
        pctile = ind.volatility_percentile(highs, lows, closes)
        row["vol_percentile"] = round(pctile, 2) if pctile is not None else None

        # The same call the analyzers make, so the reason is the real one.
        levels = scalp_levels(st.current_price, True, atr_pct, cfg, symbol=st.symbol)
        if isinstance(levels, NoTrade):
            row["verdict"] = REASON_TEXT.get(levels, levels.value)
            row["gate"] = levels.value
            out.append(row)
            continue
        row["would_target_pct"] = round(levels.target_pct * 100, 4)
        row["would_stop_pct"] = round(levels.stop_pct * 100, 4)
        row["would_take_minutes"] = round(levels.horizon_minutes)
        # The share of the risk the fee eats. Above ~0.35 the trade is mostly
        # a bet on covering its own costs, which is what the whole board was.
        row["cost_share_of_risk"] = (round(levels.cost_pct / levels.stop_pct, 3)
                                     if levels.stop_pct else None)
        row["reward_risk"] = round(levels.reward_risk, 2)
        bar = ind.median_bar_minutes([c.timestamp for c in st.candles_1m])
        row["bar_minutes"] = round(bar, 2)
        # A one-minute series whose bars are not a minute apart is not a
        # one-minute series, and every time estimate built on it is wrong by
        # that factor.
        if bar > 1.5:
            row["bar_warning"] = (f"bars are {bar:.0f} minutes apart, not 1 — "
                                  f"ATR and every time estimate are wrong by "
                                  f"about {bar:.0f}x")
        last = st.candles_1m[-1].timestamp if st.candles_1m else None
        if last is not None:
            last_aware = last if last.tzinfo else last.replace(tzinfo=UTC)
            lag = (datetime.now(UTC) - last_aware).total_seconds() / 60
            row["newest_candle_minutes_ago"] = round(lag, 1)
            if lag > 30:
                row["stale_warning"] = ("newest candle is "
                                        f"{lag / 60:.1f} hours old")
        # Closed bars only — the minute in progress has traded almost nothing,
        # so including it reports a normally trading market as having no volume.
        vols = [c.volume for c in st.candles_1m if c.is_closed]
        row["per_bar_volume"] = ind.has_usable_volume(vols)
        rel = ind.relative_volume(vols)
        row["relative_volume"] = round(rel, 2) if rel is not None else None

        v = evaluate(st.candles_1m, min_atr_pct=cfg.cost_floor_pct,
                     min_agreeing=GATE.min_agreeing, max_dissent=GATE.max_dissent)
        row["votes"] = {x.family.value: x.direction for x in v.votes}
        # Counted from the votes rather than read off the verdict. A vetoed
        # verdict has no direction, so the verdict's own tally reports 0 for
        # both sides — which hid that ETH had three families agreeing and was
        # refused by a volume reading taken from a three-second-old bar.
        longs = sum(1 for x in v.votes if x.direction > 0)
        shorts = sum(1 for x in v.votes if x.direction < 0)
        row["agreeing"] = max(longs, shorts)
        row["dissenting"] = min(longs, shorts)
        row["leaning"] = "long" if longs > shorts else ("short" if shorts > longs else "split")
        if v.direction is None:
            row["verdict"] = v.vetoes[0] if v.vetoes else "no majority"
            row["gate"] = "confluence"
        else:
            row["verdict"] = f"WOULD FIRE {v.direction} at {v.confidence * 100:.0f}%"
            row["gate"] = None
        out.append(row)

    fired = [r for r in out if r.get("gate") is None and "WOULD" in r.get("verdict", "")]
    return web.json_response({
        "checked": len(out),
        "would_fire": len(fired),
        "gate_profile": GATE.label,
        "edge_multiple": SCALP.min_edge_multiple,
        "feed": feed,
        "symbols": out,
    }, dumps=lambda o: json.dumps(o, indent=2, default=str))


async def _api_signal_history(runner, request: web.Request) -> web.Response:
    """
    Signals in a window. `before` carves out the recent end, which is what
    separates the dashboard's live 7 days from the archive behind it.
    """
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    days = max(1, min(365, int(request.query.get("days") or 7)))
    before = max(0, min(365, int(request.query.get("before") or 0)))
    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        rows = await repo.crypto_signals_between(days, before)
        counts = await repo.crypto_signal_counts() if before else None

    payload = [_signal_row(r) for r in rows]
    if counts is None:
        return web.json_response(payload)
    return web.json_response({"signals": payload, "counts": counts})


async def _api_signal_accuracy(runner, request: web.Request) -> web.Response:
    """
    Calibration, move-size distribution and per-setup accuracy.

    Buckets that have no resolved signals report a null win rate rather than
    zero — a bucket nobody has traded is not a bucket that loses.
    """
    import statistics

    from analysis.scalp_levels import ScalpConfig
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        rows = await repo.crypto_signals_between(365)
        counts = await repo.crypto_signal_counts()

    done = [r for r in rows if r.outcome in ("won", "lost")]
    moves = [abs(r.target_price - r.current_price) / r.current_price * 100
             for r in rows if r.current_price and r.target_price]

    def rate(group):
        g = [r for r in group if r.outcome in ("won", "lost")]
        if not g:
            return None, 0
        return round(sum(1 for r in g if r.outcome == "won") / len(g) * 100, 1), len(g)

    calibration = []
    for lo in (60, 65, 70, 75, 80, 85, 90):
        band = [r for r in done if lo <= r.confidence * 100 < lo + 5]
        wr, n = rate(band)
        if n:
            calibration.append({"bucket": lo, "win_rate_pct": wr, "n": n})

    by_setup = []
    for kind in sorted({r.signal_type for r in done}):
        wr, n = rate([r for r in done if r.signal_type == kind])
        if n:
            by_setup.append({"signal_type": kind, "win_rate_pct": wr, "n": n})

    edges = [0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.8, 1.2, 2.0, 5.0]
    buckets, prev = [], 0.0
    for e in edges:
        buckets.append({"upper_pct": e,
                        "n": sum(1 for m in moves if prev <= m < e)})
        prev = e

    overall, resolved = rate(rows)
    return web.json_response({
        "total": len(rows), "resolved": resolved, "pending": counts["pending"],
        "win_rate_pct": overall,
        "median_move_pct": round(statistics.median(moves), 4) if moves else None,
        "min_target_pct": round(ScalpConfig().min_target_pct * 100, 4),
        "calibration": calibration, "by_setup": by_setup, "move_buckets": buckets,
    })


async def _api_debug_volume(runner, request: web.Request) -> web.Response:
    """
    Whether each watchlist symbol has usable per-bar volume, and what the
    venue actually returns for candles.

    Written because "the volume family abstained" is invisible from outside:
    the family votes zero, the setup falls one short of the gate, and the
    signal simply never appears. This says so out loud, and prints the raw
    first candle so a changed response shape can be recognised rather than
    guessed at.
    """
    from analysis import indicators as ind
    from analysis.confluence import thin_volume_veto, volume_vote

    out = []
    states = await runner.crypto_store.get_all() if runner else []
    for st in states:
        volumes = [c.volume for c in st.candles_1m]
        highs = [c.high for c in st.candles_1m]
        lows = [c.low for c in st.candles_1m]
        closes = [c.close for c in st.candles_1m]
        usable = ind.has_usable_volume(volumes)
        vote = volume_vote(highs, lows, closes, volumes) if closes else None
        out.append({
            "symbol": st.symbol,
            "bars": len(volumes),
            "per_bar_volume_usable": usable,
            "relative_volume": ind.relative_volume(volumes),
            "volume_trend": ind.volume_trend(closes, volumes) if closes else None,
            "volume_24h": st.volume_24h,
            "vote_direction": vote.direction if vote else None,
            "vote_weight": round(vote.weight, 3) if vote else None,
            "vote_reason": vote.reason if vote else "",
            "thin_tape_veto": thin_volume_veto(volumes),
        })

    probe = None
    symbol = request.query.get("symbol")
    if symbol and runner is not None:
        got = await runner.coindcx.fetch_candles_raw(symbol, limit=3)
        if got:
            pair, payload = got
            probe = {"pair": pair, "rows": payload[:3] if isinstance(payload, list) else payload}
        else:
            probe = {"error": "no candle pair answered",
                     "tried": list(runner.coindcx._pair_variants(symbol))}

    return web.json_response({
        "note": ("A symbol with per_bar_volume_usable=false has no volume "
                 "information at all — the volume family abstains, so only "
                 "four families can vote and signals are correspondingly "
                 "rarer. Pass ?symbol=btcusdt to probe the candle endpoint."),
        "symbols": out,
        "candle_probe": probe,
    })


async def _api_audit(runner, request: web.Request) -> web.Response:
    """
    Every fired signal in a window, scored, plus the slices that explain it.

    Separate from /api/signals/accuracy on purpose: that endpoint answers
    "is the confidence number calibrated". This one answers "which predictions
    succeeded, which failed, and what do the failures have in common" — and it
    nets the round-trip cost off every figure, because the gross column is the
    one that made a leaking system look profitable.
    """
    from analysis.signal_audit import audit
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    days = max(1, min(365, int(request.query.get("days") or 30)))
    async with AsyncSessionFactory() as session:
        rows = await Repository(session).crypto_signals_between(days, limit=2000)

    report = audit(rows)
    report["days"] = days
    report["cost_model"] = {
        "round_trip_pct": round(_SCALP.cost_floor_pct * 100, 4),
        "min_target_pct": round(_SCALP.min_target_pct * 100, 4),
        "min_edge_multiple": _SCALP.min_edge_multiple,
    }
    return web.json_response(report)


async def _api_reviewer_scorecard(runner, request: web.Request) -> web.Response:
    """
    GET /api/audit/reviewer — was the AI reviewer actually right?

    Graded against resolved outcomes, including the signals it suppressed,
    which is the only way the question has an honest answer. If REJECT shows
    a better win rate than APPROVE, the reviewer is costing money.
    """
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    days = max(1, min(90, int(request.query.get("days", 14))))
    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        return web.json_response({
            "days": days,
            "scorecard": await repo.reviewer_scorecard(days),
            "post_trade_factors": await repo.review_factor_counts("post", days),
        })


async def _api_audit_methods(runner, request: web.Request) -> web.Response:
    """
    The code that produces a signal, read out of the modules themselves.

    Introspected rather than transcribed, so the page cannot describe a
    function that no longer exists or miss one that was added.
    """
    from analysis.signal_audit import method_catalogue

    return web.json_response({"stages": method_catalogue()})


async def _api_sentiment_ingest(runner, request: web.Request) -> web.Response:
    """
    Accept scored headlines from an external analyser (Hermes on a laptop).

    Push rather than pull, because the analyser runs behind a home NAT that
    this server cannot reach. Authenticated with a shared secret compared in
    constant time — a plain == leaks the secret one character at a time to
    anyone willing to measure.

    Body: {"items": [{external_id, symbol, headline, score, confidence,
                      event_type, source, url, published_at, model}, ...]}
    """
    import hmac

    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    secret = _SETTINGS.sentiment_ingest_token
    if not secret:
        return web.json_response(
            {"error": "ingest disabled", "hint": "set SENTIMENT_INGEST_TOKEN"}, status=503)

    supplied = (request.headers.get("X-Ingest-Token")
                or request.query.get("token") or "")
    if not hmac.compare_digest(supplied, secret):
        return web.json_response({"error": "unauthorised"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "body must be JSON"}, status=400)

    items = body.get("items")
    if not isinstance(items, list):
        return web.json_response({"error": "expected an 'items' list"}, status=400)
    if len(items) > 500:
        return web.json_response({"error": "at most 500 items per batch"}, status=413)

    async with AsyncSessionFactory() as session:
        accepted, duplicates = await Repository(session).ingest_news_sentiment(items)
    return web.json_response({"accepted": accepted, "duplicates": duplicates,
                              "received": len(items)})


async def _api_sentiment_recent(runner, request: web.Request) -> web.Response:
    """What the analyser has sent lately, so a score can be traced to a headline."""
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    symbol = (request.query.get("symbol") or "all").lower()
    hours = max(1, min(72, int(request.query.get("hours") or 6)))
    async with AsyncSessionFactory() as session:
        rows = await Repository(session).recent_news_sentiment(symbol, hours)
    return web.json_response([{
        "symbol": r.symbol.upper(), "headline": r.headline, "source": r.source,
        "score": round(r.score, 3), "confidence": round(r.confidence, 3),
        "event_type": r.event_type, "model": r.model, "url": r.url,
        "published_at": _iso(r.published_at),
    } for r in rows])


async def _api_debug_coindcx(runner, request: web.Request) -> web.Response:
    """
    Show exactly what CoinDCX returns for a symbol, spot and futures.

    The futures response shape is not publicly documented and could not be
    reached from the environment the collector was written in, so this exists
    to close that loop from the running server rather than by guessing.
    Add ?symbol=xauusdt to target one.
    """
    import httpx

    from collectors.coindcx import _base_symbol, _extract_price, _futures_name_variants

    symbol = (request.query.get("symbol") or "xauusdt").strip().lower()
    base = _base_symbol(symbol).upper()
    out: dict = {"symbol": symbol, "base": base}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(runner.coindcx.TICKER_URL)
        out["spot"] = {"status": r.status_code}
        if r.status_code == 200:
            rows = r.json()
            markets = {str(x.get("market", "")).upper() for x in rows}
            out["spot"]["total_markets"] = len(markets)
            out["spot"]["matching"] = sorted(m for m in markets if base in m)[:20]
    except Exception as exc:
        out["spot"] = {"error": f"{type(exc).__name__}: {exc}"}

    try:
        got = await runner.coindcx.fetch_futures_raw()
        if not got:
            out["futures"] = {"error": "no futures endpoint answered"}
        else:
            url, payload = got
            table = payload
            if isinstance(payload, dict):
                for wrapper in ("prices", "data", "result"):
                    if isinstance(payload.get(wrapper), dict):
                        table = payload[wrapper]
                        break
            out["futures"] = {"url": url, "shape": type(table).__name__}
            if isinstance(table, dict):
                keys = [str(k) for k in table]
                out["futures"]["total_instruments"] = len(keys)
                hits = [k for k in keys if base in k.upper()][:20]
                out["futures"]["matching"] = hits
                # One full sample so the price field can be identified.
                if hits:
                    out["futures"]["sample"] = {hits[0]: table[hits[0]]}
                elif keys:
                    out["futures"]["sample_any"] = {keys[0]: table[keys[0]]}
                out["futures"]["variants_tried"] = list(_futures_name_variants(base))
                out["futures"]["resolved_price"] = next(
                    (_extract_price(table.get(v)) for v in _futures_name_variants(base)
                     if _extract_price(table.get(v)) is not None), None)
            else:
                out["futures"]["raw"] = str(payload)[:1000]
    except Exception as exc:
        out["futures"] = {"error": f"{type(exc).__name__}: {exc}"}

    return web.Response(text=json.dumps(out, indent=2, default=str),
                        content_type="application/json")


async def _api_paper(runner, request: web.Request) -> web.Response:
    """
    Live state of the paper-trading cycle: wallet, open positions, trade log.

    Unrealised P&L on open positions is marked against the current price and
    reported net of the exit fee not yet paid — showing gross there would make
    every position look better than closing it would actually be.
    """
    from analysis.paper_cycle import config_for_cycle, fees_for, summarise
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        cycle = await repo.get_running_cycle()
        if cycle is None:
            recent = await repo.get_recent_cycles(limit=5)
            return web.Response(
                text=json.dumps({
                    "running": False,
                    "enabled": _SETTINGS.paper_trading_enabled,
                    "past_cycles": [_cycle_row(c) for c in recent],
                }),
                content_type="application/json")

        rows = await repo.get_open_positions(cycle.id)
        trades = await repo.get_cycle_trades(cycle.id, limit=200)
        cfg = config_for_cycle(cycle)

    states = {st.symbol: st for st in await runner.crypto_store.get_all()}
    positions = []
    unrealised_total = 0.0
    for r in rows:
        st = states.get(r.symbol)
        mark = st.current_price if st and st.current_price > 0 else r.entry_price
        sign = 1.0 if r.side == "long" else -1.0
        gross = sign * (mark - r.entry_price) * r.coin_qty * r.usdt_inr
        exit_fee = mark * r.coin_qty * r.usdt_inr * fees_for(r.symbol).effective_taker_pct
        net = gross - exit_fee
        unrealised_total += net
        positions.append({
            "symbol": r.symbol.upper(),
            "side": r.side,
            "qty": r.coin_qty,
            "entry": r.entry_price,
            "mark": mark,
            "margin": round(r.margin, 2),
            "stop": r.stop_price,
            "target": (None if (r.target_price is None or math.isinf(r.target_price)) else r.target_price),
            "liq": r.liq_price,
            "trailing": r.trail_active,
            "confidence": round(r.confidence * 100),
            "signal_type": r.signal_type,
            "unrealised": round(net, 2),
            "roe_pct": round(net / r.margin * 100, 2) if r.margin else 0.0,
            "opened_at": _iso(r.opened_at),
            # When this position times out. Exposed because a position that
            # outlives its own expiry is the visible symptom of the resolver
            # not reaching it, and that is invisible without this field.
            "expires_at": _iso(r.expires_at),
            "usdt_inr": r.usdt_inr,
            "notional": round(r.coin_qty * mark * r.usdt_inr, 2),
        })

    return web.Response(text=json.dumps({
        "running": True,
        "enabled": _SETTINGS.paper_trading_enabled,
        "cycle": _cycle_row(cycle),
        "equity": round(cycle.wallet + sum(p["margin"] for p in positions)
                        + unrealised_total, 2),
        "unrealised": round(unrealised_total, 2),
        "positions": positions,
        "summary": summarise(trades, cycle.wallet, cfg),
        "trades": [_trade_row(t) for t in trades[:60]],
        # Current rate, for figures that are not tied to one trade.
        "usdt_inr": _SETTINGS.paper_usdt_inr,
    }), content_type="application/json")


def _cycle_row(c) -> dict:
    return {
        "id": c.id,
        "status": c.status,
        "wallet": round(c.wallet, 2),
        "starting_wallet": round(c.starting_wallet, 2),
        "target_wallet": round(c.target_wallet, 2),
        "peak_wallet": round(c.peak_wallet, 2),
        "leverage": c.leverage,
        "stop_pct_of_margin": c.stop_pct_of_margin,
        "reward_risk": c.reward_risk,
        "min_confidence": c.min_confidence,
        "trailing_enabled": c.trailing_enabled,
        "scaled_sizing": c.scaled_sizing,
        "started_at": _iso(c.started_at),
        "ended_at": _iso(c.ended_at),
    }


def _trade_row(t) -> dict:
    return {
        "symbol": t.symbol.upper(),
        "side": t.side,
        "entry": t.entry_price,
        "exit": t.exit_price,
        "margin": round(t.margin, 2),
        "reason": t.exit_reason,
        "gross": round(t.gross_pnl, 2),
        "fees": round(t.trading_fees, 2),
        "funding": round(t.funding_paid, 2),
        "net": round(t.net_pnl, 2),
        "roe_pct": round(t.return_on_margin * 100, 2),
        "wallet_after": round(t.wallet_after, 2),
        "confidence": round(t.confidence * 100),
        "signal_type": t.signal_type,
        "hours_held": round(t.hours_held, 2),
        "closed_at": _iso(t.closed_at),
        "qty": t.coin_qty,
        # Money on this row is INR at the rate the trade was booked at, not
        # today's. Sent so the page converts instead of assuming.
        "usdt_inr": getattr(t, "usdt_inr", 0.0) or 102.0,
    }


async def _api_crypto_forecasts(runner, request: web.Request) -> web.Response:
    """Return multi-horizon forecasts (30m, 1h, 4h, 1d) for all watchlist symbols."""
    states = await runner.crypto_store.get_all()
    forecasts = {}
    for s in states:
        if s.current_price > 0:
            fc = runner.multi_horizon.predict_all_horizons(s)
            forecasts[s.symbol.upper()] = {
                h: {
                    "direction": f.direction,
                    "prob_up": f.probability_up,
                    "pred_change_pct": f.predicted_change_pct,
                    "confidence": f.confidence,
                    "target_price": f.target_price,
                    "support_price": f.support_price,
                    "drivers": f.key_drivers,
                }
                for h, f in fc.items()
            }
    return web.Response(text=json.dumps(forecasts), content_type="application/json")


async def _api_crypto_watchlist_add(runner, request: web.Request) -> web.Response:
    """POST /api/crypto/watchlist/add  body: {"symbol": "dogeusdt"}"""
    from scheduler.security import check_bearer_auth
    is_admin = await _verify_admin_session(request)
    if not is_admin:
        denied = check_bearer_auth(request, _SETTINGS.api_auth_token)
        if denied is not None:
            return denied
    try:
        body = await request.json()
        symbol = str(body.get("symbol", "")).strip().lower()
        if not symbol:
            return web.Response(text=json.dumps({"error": "symbol required"}),
                                 content_type="application/json", status=400)
        await runner.add_crypto_symbol(symbol)
        return web.Response(text=json.dumps({"symbol": symbol, "ok": True}),
                             content_type="application/json")
    except Exception as exc:
        return web.Response(text=json.dumps({"error": str(exc)}),
                             content_type="application/json", status=500)


async def _api_crypto_watchlist_remove(runner, request: web.Request) -> web.Response:
    """POST /api/crypto/watchlist/remove  body: {"symbol": "dogeusdt"}"""
    from scheduler.security import check_bearer_auth
    is_admin = await _verify_admin_session(request)
    if not is_admin:
        denied = check_bearer_auth(request, _SETTINGS.api_auth_token)
        if denied is not None:
            return denied
    try:
        body = await request.json()
        symbol = str(body.get("symbol", "")).strip().lower()
        if not symbol:
            return web.Response(text=json.dumps({"error": "symbol required"}),
                                 content_type="application/json", status=400)
        await runner.remove_crypto_symbol(symbol)
        return web.Response(text=json.dumps({"symbol": symbol, "ok": True}),
                             content_type="application/json")
    except Exception as exc:
        return web.Response(text=json.dumps({"error": str(exc)}),
                             content_type="application/json", status=500)


async def _api_binance_probe(runner, request: web.Request) -> web.Response:
    """
    GET /api/debug/binance — actually try to open a Binance WebSocket from THIS
    server and report what each candidate host returns.

    Binance's main host geo-blocks most US cloud IPs with HTTP 451, which no
    amount of client-side retrying can fix. Rather than guess which hosts work
    from Render, this probes them live and tells you.
    """
    import asyncio as _asyncio

    import websockets

    from collectors.binance_ws import BINANCE_WS_HOSTS, is_geoblocked

    results = []
    for host in BINANCE_WS_HOSTS:
        url = f"{host}?streams=btcusdt@kline_1m"
        entry = {"host": host}
        try:
            async with websockets.connect(url, open_timeout=8, close_timeout=3) as ws:
                msg = await _asyncio.wait_for(ws.recv(), timeout=8)
                entry.update({
                    "ok": True,
                    "verdict": "WORKS — real OHLC klines available from this server",
                    "sample_bytes": len(msg),
                })
        except Exception as exc:
            entry.update({
                "ok": False,
                "geoblocked": is_geoblocked(exc),
                "error": str(exc)[:200],
                "verdict": (
                    "GEO-BLOCKED (HTTP 451) — this server's IP is not allowed; retrying cannot help"
                    if is_geoblocked(exc)
                    else "unreachable/other error"
                ),
            })
        results.append(entry)

    any_ok = any(r.get("ok") for r in results)
    return web.Response(
        text=json.dumps({
            "any_host_reachable": any_ok,
            "recommendation": (
                "Set BINANCE_WS_ENABLED=true — a working host was found, and Binance klines "
                "carry true OHLC which makes ATR (and signal targets) far more realistic."
                if any_ok else
                "Leave Binance off. Every host is blocked from this server's IP. "
                "Use CoinDCX/CoinGecko, or redeploy in a non-blocked region."
            ),
            "hosts": results,
        }, indent=2),
        content_type="application/json",
    )


async def _api_commodities(runner, request: web.Request) -> web.Response:
    """Return real-time spot commodities (Gold, Silver, Oil)."""
    states = await runner.commodity_store.get_all()
    comms = [
        {
            "symbol": s.symbol,
            "name": s.name,
            "price": s.current_price,
            "change_24h_pct": round(s.price_change_24h_pct, 2),
            "rsi_14": s.rsi_14,
            "timestamp": _iso(s.timestamp),
        }
        for s in states
    ]
    return web.Response(text=json.dumps(comms), content_type="application/json")


async def _api_debug(runner, request: web.Request) -> web.Response:
    """Diagnostic endpoint — per-symbol feed state and collector status."""
    states = await runner.crypto_store.get_all()
    uptime = int((datetime.now(UTC) - _start_time).total_seconds())
    return web.Response(
        text=json.dumps({
            "uptime_seconds": uptime,
            "symbols": [
                {
                    "symbol": s.symbol,
                    "price": s.current_price,
                    "candles_1m": len(s.candles_1m),
                    "atr_14": s.atr_14,
                    "rsi_14": s.rsi_14,
                    "kline_status": runner.klines.status.get(s.symbol, "not fetched"),
                }
                for s in states
            ],
            "klines_host": runner.klines.host,
            "klines_last_error": runner.klines.last_error,
            "collector_status": runner.get_status(),
        }),
        content_type="application/json",
    )


async def _health(runner, request: web.Request) -> web.Response:
    # Counts priced symbols, not watchlist size: the systemd/uptime check
    # should go red when every feed is down, not merely when the DB row count
    # happens to be non-zero.
    states = await runner.crypto_store.get_all()
    priced = sum(1 for s in states if s.current_price > 0)
    uptime = int((datetime.now(UTC) - _start_time).total_seconds())
    return web.Response(
        text=json.dumps({"status": "ok", "symbols_tracked": len(states),
                         "symbols_priced": priced, "uptime_seconds": uptime}),
        content_type="application/json",
    )


# ── Dashboard HTML ────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Signal Engine</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
header{background:#1e293b;border-bottom:1px solid #334155;padding:14px 20px;display:flex;align-items:center;justify-content:space-between}
header h1{font-size:18px;font-weight:700;color:#f1f5f9;display:flex;align-items:center;gap:8px}
.badge{background:#0ea5e9;color:#fff;font-size:11px;padding:2px 8px;border-radius:9999px;font-weight:600}
.refresh{font-size:12px;color:#64748b}
.nav-btn{margin-left:12px;background:#1e293b;color:#94a3b8;font-size:13px;font-weight:600;padding:6px 14px;border-radius:8px;text-decoration:none;white-space:nowrap;border:1px solid #334155}
.nav-btn:hover{background:#334155;color:#e2e8f0}
.nav-btn.active{background:#0ea5e9;color:#fff;border-color:#0ea5e9}
.nav-btn.active:hover{background:#0284c7;color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;padding:16px 20px 0}
.card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px}
.card-title{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin-bottom:6px}
.card-value{font-size:26px;font-weight:700;color:#f1f5f9}
.card-sub{font-size:11px;color:#94a3b8;margin-top:3px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.dot-green{background:#22c55e}.dot-red{background:#ef4444}.dot-yellow{background:#f59e0b}.dot-gray{background:#475569}
section{padding:16px 20px}
section h2{font-size:12px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px}
.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.status-card{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:10px}
.status-name{font-size:11px;font-weight:600;color:#94a3b8;margin-bottom:4px}
.status-val{font-size:12px;color:#f1f5f9}
footer{text-align:center;padding:16px;color:#334155;font-size:11px;border-top:1px solid #1e293b;margin-top:4px}
.empty{color:#475569;font-size:13px;padding:20px 0;text-align:center}

/* ── Tab bar ── */
.tab-bar{display:flex;gap:0;padding:0 20px;background:#1e293b;border-bottom:2px solid #0f172a}
.tab-btn{background:none;border:none;border-bottom:3px solid transparent;color:#64748b;font-size:13px;font-weight:600;padding:11px 20px;cursor:pointer;transition:all .15s;margin-bottom:-2px}
.tab-btn.active{color:#f1f5f9;border-bottom-color:#0ea5e9}
.tab-btn:hover:not(.active){color:#94a3b8}
.tab-content{display:none}
.tab-content.active{display:block}
.tab-badge{display:inline-block;background:#dc2626;color:#fff;font-size:10px;font-weight:800;padding:0 6px;border-radius:9999px;margin-left:4px;vertical-align:middle}

/* ── Crypto tab ── */
.cr-note{font-size:11px;color:#94a3b8;line-height:1.6;background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:10px 12px;margin-bottom:12px}
.cr-watchlist-manager{display:flex;gap:8px;margin-bottom:14px}
.cr-input{flex:1;background:#0f172a;border:1px solid #334155;border-radius:8px;padding:9px 12px;color:#e2e8f0;font-size:13px}
.cr-input::placeholder{color:#475569}
.cr-add-btn{background:#0ea5e9;color:#0f172a;border:none;border-radius:8px;padding:9px 16px;font-weight:800;font-size:12px;cursor:pointer}
.cr-add-btn:hover{background:#38bdf8}
.cr-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.cr-coin{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:12px 14px}
.cr-coin-top{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.cr-coin-sym{font-size:14px;font-weight:800;color:#f1f5f9}
.cr-coin-remove{margin-left:auto;background:none;border:none;color:#475569;font-size:14px;cursor:pointer;padding:0 4px;line-height:1}
.cr-coin-remove:hover{color:#f87171}
.cr-coin-price{font-size:20px;font-weight:900;color:#f1f5f9;margin-bottom:2px}
.cr-coin-chg{font-size:12px;font-weight:700}
.cr-coin-chg.up{color:#4ade80}
.cr-coin-chg.down{color:#f87171}
.cr-coin-stats{display:flex;gap:10px;margin-top:8px;font-size:10px;color:#64748b}
.cr-coin-stats b{color:#94a3b8}
.cr-sig-card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:12px 14px;margin-bottom:10px}
.cr-sig-top{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}
.cr-sig-dir{font-size:11px;font-weight:800;padding:2px 8px;border-radius:5px}
.cr-sig-dir.long{background:#14532d;color:#4ade80}
.cr-sig-dir.short{background:#450a0a;color:#f87171}
.cr-sig-sym{font-size:13px;font-weight:800;color:#f1f5f9}
.cr-sig-name{font-size:10px;font-weight:700;color:#7dd3fc;background:#0c2140;padding:2px 7px;border-radius:4px}
.cr-sig-tf{font-size:10px;color:#64748b;margin-left:auto}
.cr-sig-when{font-size:10px;color:#64748b;margin-top:2px;font-variant-numeric:tabular-nums}
.cr-coin-nodata{opacity:.75;border-style:dashed}
.cr-nodata{font-size:14px;color:#94a3b8;font-weight:500}
.cr-sig-desc{font-size:12px;color:#94a3b8;margin-bottom:6px}
.cr-sig-row{display:flex;gap:14px;font-size:11px;color:#64748b;flex-wrap:wrap}
.cr-sig-row b{color:#e2e8f0}
.cr-sig-card.unviable{opacity:.72;border-color:#7f1d1d}
.cr-sig-warn{margin-top:8px;font-size:11px;color:#fca5a5;background:#2a1114;border:1px solid #7f1d1d;border-radius:6px;padding:7px 9px;line-height:1.5}
.cr-commodity{display:flex;gap:14px;flex-wrap:wrap}
.cr-comm-card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:12px 16px;min-width:140px}
.cr-comm-name{font-size:11px;color:#64748b;margin-bottom:4px}
.cr-comm-price{font-size:18px;font-weight:800;color:#f1f5f9}
/* Signal tabs + pagination + glossary */
.cr-sig-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.cr-tab-n{display:inline-block;margin-left:5px;font-size:9px;opacity:.65;
  font-variant-numeric:tabular-nums}
.cr-sig-tab.quiet{opacity:.55}
.cr-sig-tab{background:#1e293b;border:1px solid #334155;color:#94a3b8;font-size:11px;font-weight:700;padding:5px 12px;border-radius:9999px;cursor:pointer}
.cr-sig-tab:hover{border-color:#0ea5e9}
.cr-sig-tab.active{background:#0ea5e9;border-color:#0ea5e9;color:#0f172a}
.cr-pagination{display:flex;align-items:center;justify-content:center;gap:14px;margin-top:12px}
.cr-page-btn{background:#1e293b;border:1px solid #334155;color:#e2e8f0;font-size:12px;font-weight:700;padding:6px 14px;border-radius:8px;cursor:pointer}
.cr-page-btn:hover:not(:disabled){border-color:#0ea5e9}
.cr-page-btn:disabled{opacity:.4;cursor:default}
.cr-page-label{font-size:11px;color:#64748b}
.cr-glossary{margin-top:16px;background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:10px 14px}
.cr-glossary summary{cursor:pointer;font-size:12px;font-weight:700;color:#7dd3fc;list-style:none}
.cr-glossary summary::-webkit-details-marker{display:none}
.cr-glossary summary::before{content:'▸ ';color:#475569}
.cr-glossary[open] summary::before{content:'▾ '}
.cr-glossary dl{margin-top:10px}
.cr-glossary dt{font-size:12px;font-weight:700;color:#e2e8f0;margin-top:8px}
.cr-glossary dd{font-size:11px;color:#94a3b8;margin-top:2px;line-height:1.5}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}

.side-themes{display:flex;gap:7px;padding:10px 16px;flex-wrap:wrap}
.side-themes .dot{width:15px;height:15px;border-radius:50%;cursor:pointer;
  border:2px solid transparent;box-shadow:0 0 0 1px var(--line);transition:transform .12s}
.side-themes .dot:hover{transform:scale(1.18)}
.side-themes .dot.on{border-color:var(--text-strong);box-shadow:0 0 0 1px var(--text-strong)}

/* ── Signal cards ──────────────────────────────────────────────────────── */
/* The page was one flat slate blue end to end, which made a losing setup and
   a good one look identical at a glance. Direction now tints the card edge,
   the levels carry their own colours, and the track shows where price sits
   between stop and target without reading a single number. */
/* Two columns once there is room. One card stretched across 1140px puts the
   stop and the target so far apart they stop reading as one setup. */
.desk-link{display:flex;align-items:center;gap:13px;text-decoration:none;
  background:linear-gradient(90deg,var(--acc-t),transparent 70%),var(--panel);
  border:1px solid var(--acc-t2);border-radius:12px;padding:13px 16px;margin-bottom:14px}
.desk-link:hover{border-color:var(--accent)}
.desk-ico{font-size:21px;line-height:1}
.desk-txt{display:flex;flex-direction:column;gap:2px;min-width:0}
.desk-txt b{color:var(--text-strong);font-size:14.5px}
.desk-txt span{color:var(--muted);font-size:12px}
.desk-go{margin-left:auto;color:var(--accent);font-weight:700;font-size:13px;white-space:nowrap}
@media(max-width:620px){.desk-txt span{display:none}}
#cr-signals,#dash-signals{display:grid;gap:12px;
  grid-template-columns:repeat(auto-fill,minmax(430px,1fr))}
#cr-signals .sig,#dash-signals .sig{margin-bottom:0}
@media(max-width:900px){#cr-signals,#dash-signals{grid-template-columns:1fr}}

.sig{background:linear-gradient(180deg,var(--panel2) 0%,var(--panel) 100%);
  border:1px solid var(--line);border-left:3px solid var(--muted2);border-radius:12px;
  padding:14px 15px;display:flex;flex-direction:column;gap:11px;margin-bottom:12px}
.sig.long{border-left-color:var(--pos-strong);box-shadow:inset 0 1px 0 var(--pos-t)}
.sig.short{border-left-color:var(--neg-strong);box-shadow:inset 0 1px 0 var(--neg-t)}
.sig.unviable{border-left-color:var(--muted2);opacity:.72}

.sig-head{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.sig-sym{font-size:15px;font-weight:700;color:var(--text-strong);letter-spacing:-.01em}
.sig-dir{font-size:10px;font-weight:600;padding:3px 9px;border-radius:20px}
.sig-dir.long{background:var(--pos-t);color:var(--pos);border:1px solid var(--pos-t2)}
.sig-dir.short{background:var(--neg-t);color:var(--neg);border:1px solid var(--neg-t2)}
.sig-profit{text-align:right;background:var(--pos-t);border:1px solid var(--pos-t2);
  border-radius:9px;padding:5px 11px;line-height:1.15}
.sig-profit b{display:block;font-size:15px;color:var(--pos);font-variant-numeric:tabular-nums}
.sig-profit span{font-size:9px;color:var(--pos);text-transform:uppercase;letter-spacing:.05em}
.sig-profit.muted{background:var(--mut-t);border-color:var(--muted2)}
.sig-profit.muted b{color:var(--muted)}.sig-profit.muted span{color:var(--muted2)}

.sig-meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:10px;color:var(--muted2)}
.sig-setup{background:var(--acc-t);color:var(--accent-soft);border:1px solid var(--acc-t2);
  padding:2px 8px;border-radius:20px;font-weight:600}
.sig-when{color:var(--muted2);font-variant-numeric:tabular-nums}
.sig-horizon{color:var(--accent-soft);font-weight:600}

.sig-levels{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.sig-levels>div{display:flex;flex-direction:column;gap:1px}
.sig-levels .mid{align-items:center;text-align:center}
.sig-levels .right{align-items:flex-end;text-align:right}
.sig-levels label{font-size:9px;color:var(--muted2);text-transform:uppercase;letter-spacing:.05em}
.sig-levels b{font-size:14px;color:var(--text);font-variant-numeric:tabular-nums}
.sig-levels b.pos{color:var(--pos)}.sig-levels b.neg{color:var(--neg)}
.sig-levels span{font-size:9px;color:var(--muted2);font-variant-numeric:tabular-nums}

.sig-trail{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:12px;
  color:var(--muted);background:var(--panel2);border:1px dashed var(--line);
  border-radius:8px;padding:6px 10px;margin-top:8px}
.sig-trail b{color:var(--text-strong);font-variant-numeric:tabular-nums}
.sig-trail-k{font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--accent);font-weight:700}
.sig-track{position:relative;height:6px;background:var(--sunk);border-radius:3px;margin:2px 0 6px}
.sig-track .cap{position:absolute;top:-2px;width:4px;height:10px;border-radius:2px}
.sig-track .cap.sl{left:0;background:var(--neg-strong)}
.sig-track .cap.tp{right:0;background:var(--pos-strong)}
.sig-track .fill{position:absolute;left:0;top:0;height:6px;border-radius:3px;
  background:linear-gradient(90deg,var(--neg-t2),var(--acc-t2))}
.sig-track .now{position:absolute;top:-5px;width:0;height:0;margin-left:-5px;
  border-left:5px solid transparent;border-right:5px solid transparent;
  border-top:7px solid var(--accent2)}

.sig-foot{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.pill{font-size:9px;color:var(--muted);border:1px solid var(--line);background:var(--sunk);
  padding:3px 8px;border-radius:20px;font-variant-numeric:tabular-nums}
.pill.ok{color:var(--pos);border-color:var(--pos-t2);background:var(--pos-t)}
.pill.bad{color:var(--neg);border-color:var(--neg-t2);background:var(--neg-t)}
.sig-act{font-size:11px;font-weight:600;padding:6px 14px;border-radius:8px}
.sig-act.long{background:var(--pos-btn);color:var(--text-strong)}
.sig-act.short{background:var(--neg-btn);color:var(--text-strong)}
.sig-act.off{background:var(--panel);color:var(--muted2);border:1px solid var(--line)}
.sig-warn{background:var(--neg-t);border:1px solid var(--neg-t2);
  border-radius:8px;padding:9px 11px;font-size:10px;color:var(--neg);line-height:1.5}

/* A little colour elsewhere, so the page is not one flat field of slate. */
.card{background:linear-gradient(180deg,var(--panel) 0%,var(--panel2) 100%)}
.card-value.pos{color:var(--pos)}.card-value.neg{color:var(--neg)}
section h2{color:var(--accent-soft)}

/* ── Tables ────────────────────────────────────────────────────────────── */
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--panel);
  -webkit-overflow-scrolling:touch}
.tbl{border-collapse:collapse;width:100%;font-size:12px}
.tbl th{font-size:9px;color:var(--muted2);text-transform:uppercase;letter-spacing:.07em;
  text-align:left;padding:9px 11px;background:var(--line2);white-space:nowrap;font-weight:600}
.tbl td{padding:9px 11px;border-top:1px solid var(--bg);color:var(--text);
  font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl td.sub{color:var(--muted2);font-size:11px}
.tbl tbody tr:hover{background:var(--line2)}

/* ── Paper trading desk ────────────────────────────────────────────────── */
.ccy-pick{display:flex;align-items:center;gap:7px;margin-right:14px}
.ccy-pick label{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.ccy-pick select{font:inherit;font-size:12px;color:var(--text);background:var(--panel);
  border:1px solid var(--line);border-radius:7px;padding:5px 8px;min-width:88px}

.pt-num{font-variant-numeric:tabular-nums;white-space:nowrap;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.pt-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;
  overflow:hidden;margin-bottom:22px}
.pt-cell{background:var(--panel);padding:11px 13px}
.pt-k{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
  margin-bottom:5px}
.pt-v{font-size:16px;font-weight:600;color:var(--text-strong);
  font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.pt-v small{font-size:11px;font-weight:500;color:var(--muted);margin-left:4px}
.pt-up{color:var(--pos)!important}.pt-down{color:var(--neg)!important}
.pt-rail{height:5px;border-radius:99px;background:var(--sunk);border:1px solid var(--line2);
  overflow:hidden;margin-top:9px}
.pt-rail i{display:block;height:100%;background:var(--accent)}
.pt-railcap{display:flex;justify-content:space-between;font-size:10px;color:var(--muted);
  margin-top:5px}

.pt-shead{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:baseline;
  justify-content:space-between;margin-bottom:10px}
.pt-shead h2{font-size:12px;font-weight:600;margin:0;letter-spacing:.04em;
  text-transform:uppercase;color:var(--muted)}
.pt-count{font-size:11px;color:var(--muted2)}

.pt-scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;
  background:var(--panel)}
.pt-scroll table{border-collapse:collapse;width:100%;min-width:820px;font-size:12px}
.pt-scroll thead th{background:var(--panel2);text-align:left;font-size:10px;
  letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600;
  padding:9px 11px;border-bottom:1px solid var(--line);white-space:nowrap}
.pt-scroll thead th.sortable{cursor:pointer;user-select:none}
.pt-scroll thead th.sortable:hover{color:var(--text)}
.pt-scroll thead th .arw{opacity:.45;font-size:9px;margin-left:3px}
.pt-scroll td{padding:10px 11px;border-bottom:1px solid var(--line2);color:var(--text)}
.pt-scroll tbody tr:last-child td{border-bottom:0}
.pt-scroll tbody tr:hover{background:var(--panel2)}
.pt-scroll th.r,.pt-scroll td.r{text-align:right}
.pt-sym{font-weight:600;color:var(--text-strong)}
.pt-side{display:inline-block;font-size:9.5px;font-weight:700;padding:2px 6px;
  border-radius:4px;letter-spacing:.05em;margin-left:7px}
.pt-side.l{background:rgba(74,222,128,.14);color:var(--pos)}
.pt-side.s{background:rgba(248,113,113,.14);color:var(--neg)}
.pt-setup{font-size:10.5px;color:var(--muted);display:block;margin-top:3px}
.pt-unit{font-size:9.5px;color:var(--muted2);letter-spacing:.03em}
.pt-notional{display:block;font-size:10px;color:var(--muted2);margin-top:2px}
.pt-trail{font-size:9px;color:var(--accent);letter-spacing:.05em;
  text-transform:uppercase;margin-left:6px}
.pt-muted{color:var(--muted2)}

.pt-prail{position:relative;height:22px;min-width:130px}
.pt-ptrack{position:absolute;top:10px;left:0;right:0;height:3px;background:var(--sunk);
  border-radius:99px;border:1px solid var(--line2)}
.pt-pfill{position:absolute;top:10px;height:3px;border-radius:99px}
.pt-ptick{position:absolute;top:5px;width:1px;height:13px;background:var(--muted2)}
.pt-pmark{position:absolute;top:4px;width:2px;height:15px;border-radius:1px}
.pt-plab{position:absolute;top:0;font-size:9px;color:var(--muted2)}

.pt-tag{display:inline-block;font-size:9.5px;font-weight:700;padding:2px 7px;
  border-radius:99px;letter-spacing:.03em;white-space:nowrap;text-transform:lowercase}
.pt-tag.target,.pt-tag.trail{background:rgba(74,222,128,.14);color:var(--pos)}
.pt-tag.stop,.pt-tag.liquidated{background:rgba(248,113,113,.14);color:var(--neg)}
.pt-tag.expired{background:var(--sunk);color:var(--muted);border:1px solid var(--line)}

.pt-filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:11px;align-items:center}
.pt-filters label{font-size:10px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted);margin-right:-3px}
.pt-filters select,.pt-filters input[type=search]{font:inherit;font-size:12px;
  color:var(--text);background:var(--panel);border:1px solid var(--line);
  border-radius:7px;padding:6px 9px;min-width:100px}
.pt-filters input[type=search]{min-width:128px}
.pt-chip{font:inherit;font-size:11px;color:var(--muted);background:var(--panel);
  border:1px solid var(--line);border-radius:99px;padding:5px 11px;cursor:pointer}
.pt-chip[aria-pressed="true"]{background:var(--accent);color:var(--sunk);
  border-color:var(--accent);font-weight:700}
.pt-clear{margin-left:auto;font:inherit;font-size:11px;color:var(--muted);
  background:none;border:0;cursor:pointer;text-decoration:underline}

.pt-sum{display:grid;grid-template-columns:repeat(auto-fit,minmax(108px,1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);border-top:0;
  border-radius:0 0 10px 10px;overflow:hidden}
.pt-sum .pt-cell{padding:9px 12px}
.pt-sum .pt-v{font-size:14px}
.pt-note{font-size:11px;color:var(--muted2);margin-top:9px;line-height:1.6}
@media (max-width:640px){.pt-clear{margin-left:0}.ccy-pick label{display:none}}

/* ── Sidebar shell ─────────────────────────────────────────────────────── */
.app{display:flex;min-height:100vh}
.sidebar{width:212px;flex:none;background:var(--bg);border-right:1px solid var(--line);
  padding:16px 0;display:flex;flex-direction:column;gap:2px;position:sticky;top:0;height:100vh}
.side-brand{padding:0 16px 16px;display:flex;align-items:center;gap:9px}
.side-brand b{color:var(--text-strong);font-size:13px;font-weight:600;letter-spacing:-.01em}
.side-group{padding:12px 16px 6px;font-size:9px;font-weight:600;color:var(--muted2);
  text-transform:uppercase;letter-spacing:.09em}
.side-item{padding:7px 16px;display:flex;align-items:center;gap:9px;cursor:pointer;
  border-left:2px solid transparent;color:var(--muted);font-size:12px;text-decoration:none;
  transition:background .12s,color .12s}
.side-item:hover{background:var(--line2);color:var(--text)}
.side-item.active{background:var(--panel);border-left-color:var(--accent);color:var(--text-strong);font-weight:600}
.side-item svg{flex:none;stroke:var(--muted2)}
.side-item.active svg{stroke:var(--accent)}
.side-foot{margin-top:auto;padding:12px 16px;border-top:1px solid var(--panel);
  display:flex;flex-direction:column;gap:5px}
.side-dot{width:6px;height:6px;border-radius:50%;background:var(--pos);display:inline-block}
.main{flex-grow:1;min-width:0;display:flex;flex-direction:column}
.main-head{padding:16px 24px;border-bottom:1px solid var(--line);display:flex;
  align-items:center;gap:16px;flex-wrap:wrap}
.main-head h1{font-size:17px;font-weight:600;color:var(--text-strong);letter-spacing:-.01em;margin:0}
.main-head .sub{font-size:11px;color:var(--muted2);margin-top:2px}
.main-body{padding:20px 24px;display:flex;flex-direction:column;gap:16px}
.side-toggle{display:none}
/* Phone: the sidebar becomes a bottom bar. Icons-only in a rail would put the
   nav under the thumb-unreachable top-left corner on a 6" screen. */
@media(max-width:820px){
  .app{flex-direction:column}
  .sidebar{position:fixed;bottom:0;left:0;right:0;top:auto;width:auto;height:auto;
    flex-direction:row;border-right:none;border-top:1px solid var(--line);padding:0;
    overflow-x:auto;z-index:50;gap:0}
  .side-brand,.side-group,.side-foot,.side-themes{display:none}
  .sidebar.more-open .side-themes{display:flex;width:100%;justify-content:center;
    border-top:1px solid var(--line2);padding:9px 0}
  .side-item{flex-direction:column;gap:3px;padding:8px 14px;border-left:none;
    border-top:2px solid transparent;font-size:9px;white-space:nowrap}
  .side-item.active{border-left:none;border-top-color:var(--accent)}
  .main-body{padding:14px 12px 76px}
  .main-head{padding:12px 14px}
}

/* ── Phone ─────────────────────────────────────────────────────────────── */
/* A 9-column table inside a horizontal scroller is technically readable and
   practically useless on a 390px screen — you cannot see the symbol and the
   number at the same time. Below 640px each row becomes its own card with the
   column name beside every value, so nothing needs sideways scrolling. */
@media(max-width:640px){
  html{-webkit-text-size-adjust:100%}
  .main-body{padding:12px 11px 20px}
  footer{padding-bottom:76px}
  .main-head{padding:11px 12px}
  .main-head h1{font-size:15px}
  section h2{font-size:11px}

  .cards{grid-template-columns:1fr 1fr !important;gap:9px}
  .card{padding:11px}
  .card-value{font-size:18px}
  .card-title{font-size:10px}
  .card-sub{font-size:9px}

  .scroll{border:none;background:none;overflow-x:visible}
  .tbl,.tbl tbody,.tbl tr,.tbl td{display:block;width:100%}
  .tbl thead{display:none}
  .tbl tr{background:var(--panel);border:1px solid var(--line);border-radius:8px;
    padding:9px 11px;margin-bottom:8px}
  .tbl tr:hover{background:var(--panel)}
  .tbl td{border:none;padding:3px 0;white-space:normal;font-size:12px;
    display:flex;justify-content:space-between;align-items:baseline;gap:12px}
  .tbl td::before{content:attr(data-label);color:var(--muted2);font-size:10px;
    text-transform:uppercase;letter-spacing:.05em;flex:none}
  .tbl td:first-child{padding-bottom:6px;margin-bottom:4px;
    border-bottom:1px solid var(--bg);font-weight:600;color:var(--text-strong)}
  .tbl td:empty{display:none}

  /* Anything tapped needs a real target, not a 9px label. */
  .side-item{min-height:46px;justify-content:center}
  .cr-add-btn,.cr-page-btn,button{min-height:40px}
  .cr-input{min-height:40px;font-size:16px}   /* 16px stops iOS zooming on focus */
  .cr-sig-tab{min-height:34px;font-size:12px}
  .cr-coin-remove{min-width:32px;min-height:32px}

  .cr-grid{grid-template-columns:1fr 1fr !important}
  .cr-sig-card{padding:11px}
  .sig{padding:12px}
  .sig-sym{font-size:14px}
  .sig-levels b{font-size:13px}
  .sig-act{padding:8px 14px;min-height:38px;display:flex;align-items:center}
  .cr-note{font-size:11px}
}
/* Ten destinations do not fit a phone bar, and a sideways scroller with no
   affordance hides half of them. Five live on the bar; the rest open in a
   sheet. */
@media(max-width:640px){
  .sidebar{overflow-x:visible;justify-content:space-around}
  .side-secondary{display:none}
  .side-more{display:flex}
  .sidebar.more-open .side-secondary{display:flex}
  .sidebar.more-open{flex-wrap:wrap;padding-bottom:4px}
  .more-scrim{position:fixed;inset:0;background:rgba(0,0,0,.62);z-index:40;display:none}
  .more-scrim.on{display:block}
}
.side-more{display:none}

@media(max-width:380px){
  .cards,.cr-grid{grid-template-columns:1fr !important}
}

</style>
</head>
<body>
<div class="app">
<div class="more-scrim" id="more-scrim" onclick="toggleMore()"></div>
<nav class="sidebar" id="sidebar">
  <div class="side-brand">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#0ea5e9" stroke-width="2"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg>
    <b>Trading Desk</b>
  </div>
  <div class="side-group">Live</div>
  <div class="side-item" data-tab="dashboard" onclick="switchTab('dashboard')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h7V3H3zM14 21h7v-9h-7zM14 9h7V3h-7zM3 21h7v-6H3z"/></svg><span>Dashboard</span></div>
  <a class="side-item" data-tab="predict" href="/predict"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l3-8 4 16 3-8h4"/></svg><span>Price Outlook</span></a>
  <div class="side-item" data-tab="crypto" onclick="switchTab('crypto')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg><span>Signals</span></div>
  <div class="side-item" data-tab="paper" onclick="switchTab('paper')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M3 12h18M3 18h12"/></svg><span>Paper Trading</span></div>
  <div class="side-item" data-tab="guard" onclick="switchTab('guard')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 4v5c0 5-3.4 8.5-8 10-4.6-1.5-8-5-8-10V7z"/></svg><span>Session Guard</span></div>
  <div class="side-group">Analysis</div>
  <div class="side-item" data-tab="accuracy" onclick="switchTab('accuracy')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20V10M18 20V4M6 20v-4"/></svg><span>Accuracy</span></div>
  <div class="side-item side-secondary" data-tab="historic" onclick="switchTab('historic')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5a9 3 0 1018 0 9 3 0 10-18 0M3 5v14a9 3 0 0018 0V5"/></svg><span>Historic Data</span></div>
  <div class="side-item side-secondary" data-tab="watchlist" onclick="switchTab('watchlist')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.5 9.2l5.9-.9z"/></svg><span>Watchlist</span></div>
  <div class="side-group">Other</div>
  <a class="side-item side-secondary" data-tab="audit" href="/audit"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6v5l4 9a2 2 0 01-1.8 3H6.8A2 2 0 015 16l4-9z"/><path d="M9 8h6"/></svg><span>Signal Audit</span></a>
  <a class="side-item side-secondary" data-tab="diag" href="/api/debug/collectors"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v4M12 18v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8M2 12h4M18 12h4"/></svg><span>Diagnostics</span></a>
  <a class="side-item side-secondary" data-tab="settings" href="/settings"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 00-.1-1l2-1.6-2-3.4-2.4 1a7 7 0 00-1.7-1L14.5 3h-4l-.4 2.6a7 7 0 00-1.7 1l-2.4-1-2 3.4L6 11a7 7 0 000 2l-2 1.6 2 3.4 2.4-1a7 7 0 001.7 1l.4 2.6h4l.4-2.6a7 7 0 001.7-1l2.4 1 2-3.4-2-1.6a7 7 0 00.1-1z"/></svg><span>Settings</span></a>
  <div class="side-item side-more" onclick="toggleMore()">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>
    <span>More</span>
  </div>
  <div class="side-themes" id="side-themes">
    <span class="dot" data-t="amber"   style="background:#f59e0b" onclick="setSiteTheme('amber')"   title="Amber Terminal"></span>
    <span class="dot" data-t="carbon"  style="background:#a3e635" onclick="setSiteTheme('carbon')"  title="Carbon Lime"></span>
    <span class="dot" data-t="crimson" style="background:#fb7185" onclick="setSiteTheme('crimson')" title="Crimson"></span>
    <span class="dot" data-t="violet"  style="background:#a78bfa" onclick="setSiteTheme('violet')"  title="Violet Night"></span>
    <span class="dot" data-t="emerald" style="background:#34d399" onclick="setSiteTheme('emerald')" title="Emerald Court"></span>
    <span class="dot" data-t="navy"    style="background:#0ea5e9" onclick="setSiteTheme('navy')"    title="Deep Navy"></span>
    <span class="dot" data-t="light"   style="background:#f4f1ea" onclick="setSiteTheme('light')"   title="Polar White"></span>
  </div>
  <div class="side-foot">
    <div style="display:flex;align-items:center;gap:6px">
      <span class="side-dot" id="side-status-dot"></span>
      <span style="font-size:10px;color:#94a3b8" id="side-status">Loading&hellip;</span>
    </div>
    <div style="font-size:9px;color:#475569" id="side-uptime">&mdash;</div>
  </div>
</nav>
<div class="main">
  <div class="main-head">
    <div style="min-width:0">
      <h1 id="page-title">Dashboard</h1>
      <div class="sub" id="page-sub">Last 7 days</div>
    </div>
    <div style="flex-grow:1"></div>
    <div class="ccy-pick">
      <label for="ccy-select">Display</label>
      <select id="ccy-select" aria-label="Display currency">
        <option value="USDT">USDT</option>
        <option value="INR">&#8377; INR</option>
      </select>
    </div>
    <span class="refresh" id="refresh-label">Loading&hellip;</span>
  </div>
  <div class="main-body">


<div id="tab-dashboard" class="tab-content active">
  <a class="desk-link" href="/predict">
    <span class="desk-ico">&#128200;</span>
    <span class="desk-txt"><b>Price Outlook</b>
      <span>Where every watchlist market is likely to be in 1, 4 and 24 hours</span></span>
    <span class="desk-go">Open &rarr;</span>
  </a>
  <div class="cards" id="dash-cards"></div>
  <section>
    <h2>Predictions &middot; last 7 days</h2>
    <div class="cr-note">Anything older rolls into <b>Historic Data</b>. Move is the distance to target; &times; cost is how many round trips it covers.</div>
    <div id="dash-signals"><div class="empty">Loading&hellip;</div></div>
  </section>
  <section>
    <h2>Why signals were refused</h2>
    <div id="dash-refused"><div class="empty">Loading&hellip;</div></div>
  </section>
</div>


<div id="tab-guard" class="tab-content">
  <section>
    <h2>Session Guard</h2>
    <div class="cr-note">Behavioural flags computed from your own closed trades — size drift, hold-time drift, and how much of each gross was kept after costs.</div>
    <div class="cards" id="guard-cards"></div>
    <div id="guard-flags" style="margin-top:14px"><div class="empty">Loading&hellip;</div></div>
  </section>
  <section>
    <h2>Session drift</h2>
    <div id="guard-drift"><div class="empty">Loading&hellip;</div></div>
  </section>
</div>

<div id="tab-accuracy" class="tab-content">
  <section>
    <h2>Accuracy</h2>
    <div id="acc-banner"></div>
    <div class="cards" id="acc-cards"></div>
  </section>
  <section>
    <h2>Is confidence honest?</h2>
    <div class="cr-note">Stated confidence against realised win rate. Below the line means the bot is overconfident, and the sizing ladder is sizing on noise.</div>
    <div id="acc-calibration"><div class="empty">Loading&hellip;</div></div>
  </section>
  <section>
    <h2>Move size vs the cost floor</h2>
    <div class="cr-note">Every signal bucketed by how far its target sat. Red bars could not have paid for the round trip.</div>
    <div id="acc-moves"><div class="empty">Loading&hellip;</div></div>
  </section>
  <section>
    <h2>Accuracy by setup</h2>
    <div id="acc-setups"><div class="empty">Loading&hellip;</div></div>
  </section>
</div>

<div id="tab-historic" class="tab-content">
  <section>
    <h2>Historic Data</h2>
    <div class="cr-note">Everything older than 7 days. Read-only archive.</div>
    <div class="cards" id="hist-cards"></div>
    <div id="hist-table" style="margin-top:14px"><div class="empty">Loading&hellip;</div></div>
  </section>
</div>

<div id="tab-watchlist" class="tab-content">
  <section>
    <h2>Watchlist</h2>
    <div class="cr-note">Prices update every 30&ndash;60s from CoinDCX. Futures-only instruments such as gold come from the derivatives feed.</div>
    <div class="cr-watchlist-manager">
      <input type="text" id="cr-add-input2" class="cr-input" placeholder="Add symbol, e.g. xauusdt" onkeydown="if(event.key==='Enter')addCryptoSymbol2()">
      <button class="cr-add-btn" onclick="addCryptoSymbol2()">+ Add</button>
    </div>
    <div id="wl-coins"><div class="empty">Loading&hellip;</div></div>
  </section>
</div>

<div id="tab-paper" class="tab-content">
  <div class="cr-note" style="margin-bottom:12px">
    Simulated only — this never places a real order. A cycle ends when the wallet
    reaches its target or runs out, then a fresh one starts. Every cost is charged:
    brokerage, GST, funding and slippage.
  </div>
  <div id="paper-banner"></div>

  <div class="pt-strip" id="paper-strip"></div>

  <div class="pt-shead">
    <h2>Open positions</h2>
    <span class="pt-count" id="pt-open-count"></span>
  </div>
  <div class="pt-scroll" id="paper-positions"><div class="empty">Loading&hellip;</div></div>

  <div class="pt-shead" style="margin-top:26px">
    <h2>History</h2>
    <span class="pt-count" id="pt-hist-count"></span>
  </div>
  <div class="pt-filters" id="pt-filters">
    <label for="pf-sym">Market</label>
    <select id="pf-sym"><option value="">All</option></select>
    <label for="pf-side">Side</label>
    <select id="pf-side"><option value="">Both</option>
      <option value="long">long</option><option value="short">short</option></select>
    <label for="pf-setup">Setup</label>
    <select id="pf-setup"><option value="">All</option></select>
    <label for="pf-exit">Exit</label>
    <select id="pf-exit"><option value="">All</option></select>
    <button class="pt-chip" id="pf-win" aria-pressed="false" type="button">Wins</button>
    <button class="pt-chip" id="pf-loss" aria-pressed="false" type="button">Losses</button>
    <input type="search" id="pf-q" placeholder="Search&hellip;" aria-label="Search history">
    <button class="pt-clear" id="pf-reset" type="button">Reset</button>
  </div>
  <div class="pt-scroll" id="paper-trades"><div class="empty">Loading&hellip;</div></div>
  <div class="pt-sum" id="paper-scorecard"></div>
  <div class="pt-note">
    Costs stay broken out rather than netted into P&amp;L — fees and funding are the
    reason a winning hit rate can still lose money, so they keep their own columns.
    Summary figures follow the filters above.
  </div>
</div>

<div id="tab-crypto" class="tab-content">
  <section>
    <h2>🪙 Live Crypto Watchlist</h2>
    <div class="cr-note">Prices update every 30-60s from CoinDCX and CoinGecko. Add or remove symbols here — changes apply immediately, no redeploy needed.</div>
    <div class="cr-watchlist-manager">
      <input type="text" id="cr-add-input" class="cr-input" placeholder="Add symbol, e.g. dogeusdt" onkeydown="if(event.key==='Enter')addCryptoSymbol()">
      <button class="cr-add-btn" onclick="addCryptoSymbol()">+ Add</button>
    </div>
    <div id="cr-coins"><div class="empty">Loading watchlist…</div></div>
  </section>
  <section>
    <h2>Crypto Signals (last 24h)</h2>
    <div class="cr-sig-tabs" id="cr-sig-tabs"></div>
    <div id="cr-signals"><div class="empty">No crypto signals fired yet</div></div>
    <div class="cr-pagination" id="cr-sig-pagination"></div>
    <details class="cr-glossary">
      <summary>What do these terms mean?</summary>
      <dl>
        <dt>Momentum Reversal</dt>
        <dd>Price and momentum are disagreeing — e.g. price hits a new high but the move is losing steam. Often an early sign the current trend is running out.</dd>
        <dt>Volume Surge</dt>
        <dd>A lot more buying/selling activity than usual for this coin. Surges like this often come right before a bigger price move.</dd>
        <dt>Breakout Setup</dt>
        <dd>Price had been stuck in an unusually tight range and just broke out of it. Tight ranges tend to resolve with a sharper move than usual.</dd>
        <dt>News Catalyst</dt>
        <dd>Recent news coverage for this coin is unusually one-sided (strongly positive or negative).</dd>
        <dt>Confidence</dt>
        <dd>How strongly the model believes this signal will play out — not a guarantee. Higher is stronger, but every signal still carries risk.</dd>
        <dt>Edge</dt>
        <dd>The estimated price move (%) between the entry price and the target price.</dd>
        <dt>Entry / Target / Stop</dt>
        <dd>Suggested price to enter the trade, take profit at, and cut losses at if the trade goes the wrong way.</dd>
        <dt>Suggested stake</dt>
        <dd>A conservative position size (a small % of your bank) so no single trade risks too much — not a recommendation to trade this amount.</dd>
        <dt>RSI (Relative Strength Index)</dt>
        <dd>A 0–100 gauge of how "overbought" or "oversold" a coin is. Above 70 usually means overbought, below 30 usually means oversold.</dd>
      </dl>
    </details>
  </section>
  <section id="cr-commodities-section" style="display:none">
    <h2>Commodities</h2>
    <div id="cr-commodities"></div>
  </section>
</div>

  </div>
</div>
</div>
<footer>Auto-refreshes every 30s &middot; <span id="last-updated">&mdash;</span> &middot; <a href="/data" style="color:#38bdf8;text-decoration:none">🗄️ DB Dump</a> &middot; <a href="/settings" style="color:#3fb950;text-decoration:none">⚙️ Settings</a> &middot; <a href="/api/debug" style="color:#a78bfa;text-decoration:none">🔬 Debug</a></footer>

<script>

function fmtUptime(s){if(s==null||isNaN(s))return '—';if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h+'h '+m+'m';}
const _IST={timeZone:'Asia/Kolkata'};
function fmtTime(iso){
  const d=new Date(iso.endsWith('Z')||iso.includes('+')?iso:iso+'Z');
  return d.toLocaleTimeString('en-IN',{..._IST,hour:'2-digit',minute:'2-digit'})+ ' IST';
}

function esc(s){
  const d=document.createElement('div');
  d.textContent=s||'';
  return d.innerHTML;
}

// ── TAB SWITCHING ─────────────────────────────────────────────────────────────

// ── PAPER TRADING ─────────────────────────────────────────────────────────────
const PAPER_REASON = {
  target:'hit target', stop:'stopped out', liquidation:'LIQUIDATED',
  expiry:'time expired', cycle_end:'cycle closed', signal_flip:'setup reversed',
  conviction_lost:'conviction faded', market_shock:'market shock'
};
function money(v){
  const sign = v < 0 ? '-' : '';
  return sign + '₹' + Math.abs(v).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
}
function signed(v){ return (v>=0?'+':'') + money(v).replace('-',''); }
function pnlClass(v){ return v>0?'pos':(v<0?'neg':''); }

// ── Paper trading desk ──────────────────────────────────────────────────
// One fetch feeds three views: the cycle strip, open positions and a
// filtered history whose summary recomputes against whatever is showing —
// so "how does volume_spike actually do" is a two-click question rather
// than a database query.
let _pt = null;                       // last payload, kept so filtering and a
                                      // currency change re-render without refetching
const _ptF = {sym:'', side:'', setup:'', exit:'', win:false, loss:false, q:''};
let _ptSort = {key:'closed_at', dir:-1};

function _ptMoney(v, rate, signed){ return window.Money.fmt(v, rate, signed); }
function _ptCls(n){ return n > 0 ? 'pt-up' : n < 0 ? 'pt-down' : ''; }
function _ptQty(q){
  if(!q) return '0';
  const dp = q >= 1000 ? 2 : q >= 1 ? 4 : 6;
  return q.toFixed(dp);
}
// A position past its own expiry is still open only if nothing resolved it.
// Saying "overdue" is the difference between noticing that and not.
function _ptExpiry(iso){
  if(!iso) return '<span class="pt-down">no expiry</span>';
  const mins = (new Date(iso) - Date.now()) / 60000;
  if(mins < 0) return `<span class="pt-down">overdue ${Math.round(-mins)}m</span>`;
  return `<span class="pt-muted">in ${Math.round(mins)}m</span>`;
}

async function loadPaper(){
  let d;
  try { d = await (await fetch('/api/paper')).json(); }
  catch(e){
    document.getElementById('paper-banner').innerHTML =
      '<div class="cr-sig-warn">Could not reach /api/paper.</div>';
    return;
  }
  _pt = d;
  renderPaper();
}

function renderPaper(){
  const d = _pt;
  if(!d) return;
  const banner = document.getElementById('paper-banner');

  if(!d.enabled){
    banner.innerHTML = '<div class="cr-sig-warn">Paper trading is switched off — '
      + 'turn it on in <a href="/settings">Settings</a>.</div>';
  } else if(!d.running){
    banner.innerHTML = '<div class="cr-note">No cycle running — one starts on the next tick.</div>';
  } else { banner.innerHTML = ''; }

  if(!d.running){
    document.getElementById('paper-strip').innerHTML = '';
    document.getElementById('paper-positions').innerHTML = '<div class="empty">No open positions</div>';
    document.getElementById('paper-trades').innerHTML = '<div class="empty">No trades yet</div>';
    document.getElementById('paper-scorecard').innerHTML = '';
    document.getElementById('pt-open-count').textContent = '';
    document.getElementById('pt-hist-count').textContent = '';
    return;
  }

  const c = d.cycle, rate = d.usdt_inr || 102;
  const margin = (d.positions || []).reduce((a, p) => a + p.margin, 0);
  // Margin locked in an open position has left the wallet but has not been
  // lost. Measuring realised P&L as wallet-minus-start counts it as a loss:
  // a 554 margin made a -252 cycle read as -806, three times worse than the
  // truth, and the error grows with every position left open.
  const realised = c.wallet + margin - c.starting_wallet;
  const span = Math.max(1, c.target_wallet - c.starting_wallet);
  const pct = Math.max(0, Math.min(100, (c.wallet - c.starting_wallet) / span * 100));

  document.getElementById('paper-strip').innerHTML = `
    <div class="pt-cell"><div class="pt-k">Equity</div>
      <div class="pt-v">${_ptMoney(d.equity, rate)}</div></div>
    <div class="pt-cell"><div class="pt-k">Wallet</div>
      <div class="pt-v">${_ptMoney(c.wallet, rate)}</div></div>
    <div class="pt-cell"><div class="pt-k">Unrealised</div>
      <div class="pt-v ${_ptCls(d.unrealised)}">${_ptMoney(d.unrealised, rate, true)}</div></div>
    <div class="pt-cell"><div class="pt-k">Realised P&amp;L</div>
      <div class="pt-v ${_ptCls(realised)}">${_ptMoney(realised, rate, true)}</div></div>
    <div class="pt-cell"><div class="pt-k">Margin in use</div>
      <div class="pt-v">${_ptMoney(margin, rate)}<small> · ${(d.positions||[]).length} open</small></div></div>
    <div class="pt-cell"><div class="pt-k">Cycle ${c.id} · ${c.leverage}&times;</div>
      <div class="pt-v" style="font-size:13px">${_ptMoney(c.starting_wallet, rate)} &rarr; ${_ptMoney(c.target_wallet, rate)}</div>
      <div class="pt-rail"><i style="width:${pct.toFixed(1)}%"></i></div>
      <div class="pt-railcap"><span>${pct.toFixed(0)}% there</span>
        <span>${window.Money.get() === 'INR' ? 'figures in &#8377;' : '1 USDT = &#8377;' + rate}</span></div></div>`;

  renderPaperPositions(d.positions || [], rate);
  renderPaperHistory();
}

function renderPaperPositions(rows, rate){
  const el = document.getElementById('paper-positions');
  document.getElementById('pt-open-count').textContent =
    rows.length ? rows.length + ' open' : '';
  if(!rows.length){ el.innerHTML = '<div class="empty">No open positions</div>'; return; }

  el.innerHTML = '<table><thead><tr>'
    + '<th>Market</th><th class="r">Quantity</th><th class="r">Entry</th><th class="r">Mark</th>'
    + '<th>Stop &middot; Target</th><th class="r">Margin</th><th class="r">Liq.</th>'
    + '<th class="r">Unrealised</th><th class="r">ROE</th><th class="r">Opened</th>'
    + '<th class="r">Expires</th>'
    + '</tr></thead><tbody>'
    + rows.map(p => {
        const long = p.side === 'long';
        const lo = Math.min(p.stop, p.target), hi = Math.max(p.stop, p.target);
        const span = Math.max(1e-9, hi - lo);
        const at = x => Math.max(0, Math.min(100, (x - lo) / span * 100));
        const eAt = at(p.entry), mAt = at(p.mark);
        const good = long ? p.mark >= p.entry : p.mark <= p.entry;
        const col = good ? 'var(--pos)' : 'var(--neg)';
        const fillL = Math.min(eAt, mAt), fillW = Math.abs(mAt - eAt);
        const unit = p.symbol.replace(/USDT$/, '');
        return `<tr>
          <td><span class="pt-sym">${p.symbol}</span>`
          + `<span class="pt-side ${long ? 'l' : 's'}">${p.side.toUpperCase()}</span>`
          + (p.trailing ? '<span class="pt-trail">trailing</span>' : '')
          + `<span class="pt-setup">${(p.signal_type||'').replace(/_/g,' ')} &middot; ${p.confidence}%</span></td>
          <td class="r pt-num">${_ptQty(p.qty)} <span class="pt-unit">${unit}</span>
            <span class="pt-notional">${_ptMoney(p.notional, rate)}</span></td>
          <td class="r pt-num">${p.entry}</td>
          <td class="r pt-num ${good ? 'pt-up' : 'pt-down'}">${p.mark}</td>
          <td><div class="pt-prail">
              <span class="pt-ptrack"></span>
              <span class="pt-pfill" style="left:${fillL}%;width:${fillW}%;background:${col}"></span>
              <span class="pt-ptick" style="left:${eAt}%"></span>
              <span class="pt-pmark" style="left:${mAt}%;background:${col}"></span>
              <span class="pt-plab" style="left:0">${long ? p.stop : p.target}</span>
              <span class="pt-plab" style="right:0">${long ? p.target : p.stop}</span>
            </div></td>
          <td class="r pt-num">${_ptMoney(p.margin, rate)}</td>
          <td class="r pt-num pt-muted">${p.liq}</td>
          <td class="r pt-num ${_ptCls(p.unrealised)}">${_ptMoney(p.unrealised, rate, true)}</td>
          <td class="r pt-num ${_ptCls(p.roe_pct)}">${p.roe_pct > 0 ? '+' : ''}${p.roe_pct}%</td>
          <td class="r pt-num pt-muted">${fmtTime(p.opened_at)}</td>
          <td class="r pt-num">${_ptExpiry(p.expires_at)}</td>
        </tr>`;
      }).join('')
    + '</tbody></table>';
}

function _ptFill(id, values){
  const sel = document.getElementById(id);
  if(!sel) return;
  const keep = sel.value;
  const first = sel.options[0];
  sel.innerHTML = '';
  sel.appendChild(first);
  values.forEach(v => {
    const o = document.createElement('option');
    o.value = v; o.textContent = String(v).replace(/_/g, ' ');
    sel.appendChild(o);
  });
  if(values.includes(keep)) sel.value = keep;
}

function renderPaperHistory(){
  const d = _pt;
  if(!d || !d.running) return;
  const all = d.trades || [], rate = d.usdt_inr || 102;

  _ptFill('pf-sym',   [...new Set(all.map(t => t.symbol))].sort());
  _ptFill('pf-setup', [...new Set(all.map(t => t.signal_type).filter(Boolean))].sort());
  _ptFill('pf-exit',  [...new Set(all.map(t => t.reason).filter(Boolean))].sort());

  const q = _ptF.q.toLowerCase();
  let rows = all.filter(t =>
    (!_ptF.sym   || t.symbol === _ptF.sym) &&
    (!_ptF.side  || t.side === _ptF.side) &&
    (!_ptF.setup || t.signal_type === _ptF.setup) &&
    (!_ptF.exit  || t.reason === _ptF.exit) &&
    (!_ptF.win   || t.net > 0) &&
    (!_ptF.loss  || t.net <= 0) &&
    (!q || ((t.symbol + ' ' + (t.signal_type||'') + ' ' + (t.reason||'')).toLowerCase().includes(q)))
  );

  const k = _ptSort.key, dir = _ptSort.dir;
  rows = rows.slice().sort((a, b) => {
    const x = a[k], y = b[k];
    if(x === y) return 0;
    return (x > y ? 1 : -1) * dir;
  });

  document.getElementById('pt-hist-count').textContent =
    `showing ${rows.length} of ${all.length} closed`;

  const th = (key, label, right) =>
    `<th class="sortable${right ? ' r' : ''}" data-sort="${key}">${label}`
    + (k === key ? `<span class="arw">${dir > 0 ? '&uarr;' : '&darr;'}</span>` : '') + '</th>';

  const el = document.getElementById('paper-trades');
  el.innerHTML = rows.length
    ? '<table><thead><tr>'
      + th('closed_at','Closed') + '<th>Market</th>'
      + '<th class="r">Quantity</th><th class="r">Entry &rarr; Exit</th>'
      + th('reason','Exit') + th('gross','Gross',1) + '<th class="r">Fees</th>'
      + '<th class="r">Funding</th>' + th('net','Net',1) + th('roe_pct','ROE',1)
      + th('hours_held','Held',1) + '<th class="r">Wallet</th>'
      + '</tr></thead><tbody>'
      + rows.map(t => {
          const r = t.usdt_inr || rate;
          const unit = t.symbol.replace(/USDT$/, '');
          return `<tr>
            <td class="pt-num pt-muted">${fmtTime(t.closed_at)}</td>
            <td><span class="pt-sym">${t.symbol}</span>`
            + `<span class="pt-side ${t.side === 'long' ? 'l' : 's'}">${t.side.toUpperCase()}</span>`
            + `<span class="pt-setup">${(t.signal_type||'').replace(/_/g,' ')} &middot; ${t.confidence}%</span></td>
            <td class="r pt-num">${_ptQty(t.qty)} <span class="pt-unit">${unit}</span></td>
            <td class="r pt-num">${t.entry} <span class="pt-muted">&rarr;</span> ${t.exit}</td>
            <td><span class="pt-tag ${t.reason}">${t.reason}</span></td>
            <td class="r pt-num ${_ptCls(t.gross)}">${_ptMoney(t.gross, r, true)}</td>
            <td class="r pt-num pt-muted">${_ptMoney(-t.fees, r)}</td>
            <td class="r pt-num pt-muted">${_ptMoney(-t.funding, r)}</td>
            <td class="r pt-num ${_ptCls(t.net)}" style="font-weight:600">${_ptMoney(t.net, r, true)}</td>
            <td class="r pt-num ${_ptCls(t.roe_pct)}">${t.roe_pct > 0 ? '+' : ''}${t.roe_pct}%</td>
            <td class="r pt-num">${t.hours_held < 1 ? Math.round(t.hours_held*60) + 'm' : t.hours_held + 'h'}</td>
            <td class="r pt-num pt-muted">${_ptMoney(t.wallet_after, r)}</td>
          </tr>`;
        }).join('')
      + '</tbody></table>'
    : '<div class="empty">No trades match these filters</div>';

  el.querySelectorAll('th.sortable').forEach(h => h.addEventListener('click', () => {
    const key = h.dataset.sort;
    _ptSort = {key: key, dir: _ptSort.key === key ? -_ptSort.dir : -1};
    renderPaperHistory();
  }));

  const filtered = !!(_ptF.sym || _ptF.side || _ptF.setup || _ptF.exit
                      || _ptF.win || _ptF.loss || _ptF.q);
  renderPaperSummary(rows, rate, filtered);
}

function renderPaperSummary(rows, rate, filtered){
  const el = document.getElementById('paper-scorecard');
  if(!rows.length){ el.innerHTML = ''; return; }
  const n = rows.length;
  const wins = rows.filter(t => t.net > 0), losses = rows.filter(t => t.net <= 0);
  const net = rows.reduce((a,t) => a + t.net, 0);
  const gross = rows.reduce((a,t) => a + t.gross, 0);
  const grossWon = rows.reduce((a,t) => a + Math.max(0, t.gross), 0);
  const fees = rows.reduce((a,t) => a + t.fees, 0);
  const funding = rows.reduce((a,t) => a + t.funding, 0);
  const avgW = wins.length ? wins.reduce((a,t)=>a+t.net,0)/wins.length : 0;
  const avgL = losses.length ? losses.reduce((a,t)=>a+t.net,0)/losses.length : 0;
  let streak = 0, worst = 0;
  rows.slice().sort((a,b) => (a.closed_at > b.closed_at ? 1 : -1))
      .forEach(t => { streak = t.net <= 0 ? streak+1 : 0; worst = Math.max(worst, streak); });

  // Unfiltered, the server's figure wins: /api/paper returns only the most
  // recent trades, so a ratio computed here would quietly describe a window
  // rather than the cycle. Filtered, the window IS the question being asked.
  const srv = (_pt && _pt.summary) ? _pt.summary.costs_as_pct_of_gross : null;
  const costRatio = (!filtered && srv !== null && srv !== undefined)
    ? srv.toFixed(1) + '%'
    : (grossWon > 0 ? ((fees + funding) / grossWon * 100).toFixed(1) + '%' : '—');

  const cell = (k, v, cls) =>
    `<div class="pt-cell"><div class="pt-k">${k}</div><div class="pt-v ${cls||''}">${v}</div></div>`;
  el.innerHTML =
      cell('Win rate', (wins.length / n * 100).toFixed(1) + '%')
    + cell('Gross P&amp;L', _ptMoney(gross, rate, true), _ptCls(gross))
    + cell('Trading fees', _ptMoney(-fees, rate), 'pt-down')
    + cell('Funding', _ptMoney(-funding, rate), 'pt-down')
    + cell('Net P&amp;L', _ptMoney(net, rate, true), _ptCls(net))
    + cell('Expectancy', _ptMoney(net / n, rate, true), _ptCls(net))
    + cell('Realised R:R', avgL ? Math.abs(avgW/avgL).toFixed(2) : '—')
    + cell('Costs / gross', costRatio)
    + cell('Worst streak', worst);
}

(function wirePaperFilters(){
  const bind = (id, key) => {
    const el = document.getElementById(id);
    if(el) el.addEventListener('input', () => { _ptF[key] = el.value; renderPaperHistory(); });
  };
  bind('pf-sym','sym'); bind('pf-side','side'); bind('pf-setup','setup');
  bind('pf-exit','exit'); bind('pf-q','q');
  [['pf-win','win','pf-loss','loss'], ['pf-loss','loss','pf-win','win']]
    .forEach(([id, key, otherId, otherKey]) => {
      const b = document.getElementById(id);
      if(!b) return;
      b.addEventListener('click', () => {
        _ptF[key] = !_ptF[key];
        b.setAttribute('aria-pressed', String(_ptF[key]));
        if(_ptF[key]){
          _ptF[otherKey] = false;
          document.getElementById(otherId).setAttribute('aria-pressed','false');
        }
        renderPaperHistory();
      });
    });
  const reset = document.getElementById('pf-reset');
  if(reset) reset.addEventListener('click', () => {
    Object.assign(_ptF, {sym:'',side:'',setup:'',exit:'',win:false,loss:false,q:''});
    ['pf-sym','pf-side','pf-setup','pf-exit','pf-q'].forEach(i => {
      const e = document.getElementById(i); if(e) e.value = '';
    });
    ['pf-win','pf-loss'].forEach(i => {
      const e = document.getElementById(i); if(e) e.setAttribute('aria-pressed','false');
    });
    renderPaperHistory();
  });
  // A currency change is a repaint, never a refetch.
  document.addEventListener('ccychange', () => { if(_pt) renderPaper(); });
})();


// Copy each table's column names onto its cells so the phone layout can show
// them beside the values. Cheap, idempotent, and keeps every table builder
// free of presentation concerns.
function labelTables(root){
  (root||document).querySelectorAll('table.tbl').forEach(t=>{
    const heads=[...t.querySelectorAll('thead th')].map(th=>th.textContent.trim());
    if(!heads.length) return;
    t.querySelectorAll('tbody tr').forEach(tr=>{
      [...tr.children].forEach((td,i)=>{
        if(heads[i] && !td.hasAttribute('data-label')) td.setAttribute('data-label',heads[i]);
      });
    });
  });
}
// One observer instead of a call at the end of every render function — the
// tables are built in a dozen places and one missed call is an unlabelled
// table on a phone with no other symptom.
new MutationObserver(()=>labelTables()).observe(document.documentElement,
  {childList:true,subtree:true});

// ── SIDEBAR VIEWS ─────────────────────────────────────────────────────────
// Everything below computes from endpoints that already exist. Where a number
// genuinely cannot be produced yet it says so rather than showing a zero.

function card(label,value,sub,cls){
  return `<div class="card"><div class="card-title">${label}</div>
    <div class="card-value ${cls||''}">${value}</div><div class="card-sub">${sub||''}</div></div>`;
}
function moveOf(s){
  return (s.target_price && s.current_price)
    ? Math.abs(s.target_price - s.current_price) / s.current_price * 100 : 0;
}
function xCost(s){ return moveOf(s) / BREAK_EVEN_PCT; }

async function loadDashboard(){
  const [sigs,paper] = await Promise.all([jget('/api/signals/history?days=7',[]), jget('/api/paper',{})]);
  const taken = sigs.filter(s=>xCost(s) >= MIN_TARGET_PCT/BREAK_EVEN_PCT);
  const eq = paper.running ? paper.equity : null;
  const s = paper.summary || {};
  document.getElementById('dash-cards').innerHTML =
      card('Equity', eq==null?'&mdash;':money(eq),
           paper.running?`from ${money(paper.cycle.starting_wallet)}`:'no cycle running',
           eq!=null && paper.running && eq>=paper.cycle.starting_wallet?'pos':'')
    + card('Signals, 7d', sigs.length, `${taken.length} viable · ${sigs.length-taken.length} refused`)
    + card('Hit rate, 7d', s.win_rate_pct!=null?s.win_rate_pct+'%':'&mdash;',
           s.trades?`${s.trades} closed trades`:'needs closed trades')
    + card('Costs, 7d', s.trading_fees!=null?money(s.trading_fees+s.funding_paid):'&mdash;',
           s.costs_as_pct_of_gross!=null?s.costs_as_pct_of_gross+'% of gross':'', 'neg');

  document.getElementById('dash-signals').innerHTML = sigs.length ? `
    <div class="scroll"><table class="tbl"><thead><tr>
      <th>Fired</th><th>Symbol</th><th>Setup</th><th>Dir</th><th>Move</th><th>&times; cost</th><th>Conf</th>
    </tr></thead><tbody>` + sigs.slice(0,40).map(x=>{
      const m=moveOf(x), xc=xCost(x), ok=m>=MIN_TARGET_PCT;
      return `<tr>
        <td class="sub">${fmtSignalTime(x.timestamp)}</td>
        <td><b>${esc(x.symbol)}</b></td>
        <td>${esc(CR_SIG_NAME[x.signal_type]||x.signal_type)}</td>
        <td class="${x.direction==='long'?'pos':'neg'}">${x.direction.toUpperCase()}</td>
        <td>${m.toFixed(3)}%</td>
        <td class="${ok?'pos':'neg'}">${xc.toFixed(1)}&times;</td>
        <td>${x.confidence}%</td></tr>`;
    }).join('') + '</tbody></table></div>'
    : '<div class="empty">No signals in the last 7 days</div>';

  const refused = {};
  sigs.forEach(x=>{ if(moveOf(x) < MIN_TARGET_PCT) refused['below the cost floor'] = (refused['below the cost floor']||0)+1; });
  const rows = Object.entries(refused);
  document.getElementById('dash-refused').innerHTML = rows.length
    ? rows.map(([k,v])=>`<div style="display:flex;align-items:center;gap:10px;padding:4px 0">
        <div style="height:6px;background:#334155;border-radius:3px;width:${Math.min(240,v*12)}px"></div>
        <span style="font-size:11px;color:#94a3b8">${esc(k)} · ${v}</span></div>`).join('')
    : '<div class="empty">Nothing refused in this window</div>';
}

async function loadWatchlist(){
  const coins = await jget('/api/crypto/coins',[]);
  const el = document.getElementById('wl-coins');
  const prev = document.getElementById('cr-coins');
  renderCryptoCoins.call(null, coins);
  el.innerHTML = prev ? prev.innerHTML : '<div class="empty">No symbols</div>';
}

async function loadGuard(){
  const paper = await jget('/api/paper',{});
  const trades = (paper.trades||[]).slice().reverse();   // oldest first
  if(!trades.length){
    document.getElementById('guard-cards').innerHTML='';
    document.getElementById('guard-flags').innerHTML='<div class="empty">No closed trades yet — flags appear once a session has trades to compare.</div>';
    document.getElementById('guard-drift').innerHTML='';
    return;
  }
  const notional = t => Math.abs(t.margin) * (t.leverage||1);
  const kept = t => t.gross ? (t.net/t.gross*100) : null;
  const first = trades[0], last = trades[trades.length-1];

  const sizeDrift = notional(last)/notional(first)-1;
  const keptFirst = kept(first), keptLast = kept(last);
  document.getElementById('guard-cards').innerHTML =
      card('Trades', trades.length, 'this cycle')
    + card('Size trend', (sizeDrift>=0?'+':'')+(sizeDrift*100).toFixed(0)+'%',
           `${money(notional(first))} → ${money(notional(last))}`, sizeDrift>0.25?'neg':'')
    + card('Median hold', fmtHours(median(trades.map(t=>t.hours_held))), 'per trade')
    + card('Kept of gross', keptLast==null?'&mdash;':keptLast.toFixed(0)+'%',
           keptFirst!=null?`was ${keptFirst.toFixed(0)}% on the first`:'',
           keptLast!=null && keptLast<60?'neg':'pos');

  const flags=[];
  if(sizeDrift > 0.25 && keptLast!=null && keptFirst!=null && keptLast < keptFirst)
    flags.push(['red','Escalating size while the edge shrank',
      'Each trade got larger while less of the gross survived costs. Fees scale with size; the edge did not.',
      `${money(notional(first))} → ${money(notional(last))} · kept ${keptFirst.toFixed(0)}% → ${keptLast.toFixed(0)}%`]);
  const thin = trades.filter(t=>t.gross && Math.abs(t.gross)>0 && (t.fees+t.funding)/Math.abs(t.gross) > 0.33);
  if(thin.length)
    flags.push(['red','Trades where costs took a third or more',
      'At this size the round trip is eating the result. The target has to clear the cost floor by a wide margin, not by a hair.',
      thin.map(t=>`${t.symbol} ${money(t.gross)} gross, ${money(t.fees+t.funding)} fees`).join(' · ')]);
  const quick = trades.filter(t=>t.hours_held!=null && t.hours_held < 0.1);
  if(quick.length >= 2)
    flags.push(['amber','Several trades held under six minutes',
      'Short holds capture small moves, and a small move is where the fee share is largest.',
      `${quick.length} of ${trades.length} trades`]);
  const flips=[];
  for(let i=1;i<trades.length;i++){
    const a=trades[i-1], b=trades[i];
    if(a.symbol===b.symbol && a.side!==b.side &&
       Math.abs(new Date(b.closed_at)-new Date(a.closed_at)) < 45*60*1000)
      flips.push(`${a.symbol} ${a.side}→${b.side}`);
  }
  if(flips.length)
    flags.push(['amber','Direction reversed in the same symbol within the hour',
      'Closing one side and opening the other shortly after usually means the exit was about discomfort rather than the setup changing.',
      flips.join(' · ')]);
  if(!flags.length)
    flags.push(['ok','Nothing to flag',
      'Size, hold time and cost share are all steady across this session.',
      `${trades.length} trades compared`]);

  const COL={red:['#f87171','#3f1d1d'],amber:['#fbbf24','#3a2f14'],ok:['#4ade80','#14532d']};
  document.getElementById('guard-flags').innerHTML = flags.map(([lv,t,d,e])=>{
    const [c,bg]=COL[lv];
    return `<div style="border:1px solid ${c};background:${bg};border-radius:8px;padding:11px 13px;margin-bottom:9px">
      <div style="font-size:12px;font-weight:600;color:#f1f5f9">${esc(t)}</div>
      <div style="font-size:11px;color:#cbd5e1;margin-top:3px">${esc(d)}</div>
      <div style="font-size:10px;color:#94a3b8;margin-top:5px">${esc(e)}</div></div>`;
  }).join('');

  document.getElementById('guard-drift').innerHTML = `
    <div class="scroll"><table class="tbl"><thead><tr>
      <th>#</th><th>Symbol</th><th>Notional</th><th>Held</th><th>Gross</th><th>Costs</th><th>Kept</th>
    </tr></thead><tbody>` + trades.map((t,i)=>{
      const k=kept(t);
      return `<tr><td class="sub">${i+1}</td><td><b>${esc(t.symbol)}</b></td>
        <td>${money(notional(t))}</td><td>${fmtHours(t.hours_held)}</td>
        <td class="${t.gross>=0?'pos':'neg'}">${signed(t.gross)}</td>
        <td class="neg">-${money(t.fees+t.funding)}</td>
        <td class="${k!=null&&k<60?'neg':'pos'}">${k==null?'&mdash;':k.toFixed(0)+'%'}</td></tr>`;
    }).join('') + '</tbody></table></div>';
}

function median(xs){ const v=xs.filter(x=>x!=null).sort((a,b)=>a-b); return v.length?v[Math.floor(v.length/2)]:null; }
function fmtHours(h){
  if(h==null) return '&mdash;';
  if(h<1/60) return Math.round(h*3600)+'s';
  if(h<1) return Math.round(h*60)+'m';
  return h.toFixed(1)+'h';
}

async function loadAccuracy(){
  const stats = await jget('/api/signals/accuracy',{});
  const banner = document.getElementById('acc-banner');
  if(!stats.resolved){
    banner.innerHTML = `<div class="cr-sig-warn">Outcomes are not resolved yet, so accuracy cannot be computed.
      ${stats.pending||0} signals are stored as pending. The resolver walks stored candles forward from
      each signal to see whether target or stop came first — until it runs, every chart here is empty rather than wrong.</div>`;
  } else { banner.innerHTML=''; }

  document.getElementById('acc-cards').innerHTML =
      card('Signals', stats.total||0, 'in the archive')
    + card('Resolved', stats.resolved||0, stats.pending?`${stats.pending} pending`:'')
    + card('Win rate', stats.win_rate_pct!=null?stats.win_rate_pct+'%':'&mdash;', 'of resolved')
    + card('Median move', stats.median_move_pct!=null?stats.median_move_pct.toFixed(3)+'%':'&mdash;',
           stats.median_move_pct!=null?(stats.median_move_pct/BREAK_EVEN_PCT).toFixed(1)+'× cost':'');

  document.getElementById('acc-calibration').innerHTML =
    (stats.calibration||[]).length ? barRows((stats.calibration||[]).map(b=>
      [`${b.bucket}% stated`, b.win_rate_pct, `${b.win_rate_pct}% · n=${b.n}`]))
    : '<div class="empty">Needs resolved outcomes</div>';

  document.getElementById('acc-moves').innerHTML =
    (stats.move_buckets||[]).length ? moveHistogram(stats.move_buckets)
    : '<div class="empty">Needs archived signals</div>';

  document.getElementById('acc-setups').innerHTML =
    (stats.by_setup||[]).length ? barRows((stats.by_setup||[]).map(b=>
      [esc(CR_SIG_NAME[b.signal_type]||b.signal_type), b.win_rate_pct, `${b.win_rate_pct}% · n=${b.n}`]))
    : '<div class="empty">Needs resolved outcomes</div>';
}

function barRows(rows){
  const mx = Math.max(...rows.map(r=>r[1]), 1);
  return '<div style="display:flex;flex-direction:column;gap:8px">' + rows.map(([lab,v,note])=>
    `<div style="display:grid;grid-template-columns:130px 1fr 92px;align-items:center;gap:10px">
      <span style="font-size:11px;color:#94a3b8">${lab}</span>
      <div style="height:8px;background:#0f172a;border-radius:4px;overflow:hidden">
        <div style="height:8px;width:${v/mx*100}%;background:#0ea5e9;border-radius:4px"></div></div>
      <span style="font-size:10px;color:#64748b;text-align:right">${note}</span></div>`).join('') + '</div>';
}

function moveHistogram(buckets){
  const mx = Math.max(...buckets.map(b=>b.n), 1);
  return '<div style="display:flex;align-items:flex-end;gap:5px;height:130px">' + buckets.map(b=>{
    const under = b.upper_pct < MIN_TARGET_PCT;
    return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:5px;height:100%;justify-content:flex-end">
      <div style="width:100%;height:${b.n/mx*100}%;background:${under?'#f87171':'#0ea5e9'};border-radius:2px 2px 0 0"
           title="${b.n} signals"></div>
      <span style="font-size:9px;color:#475569">${b.upper_pct}%</span></div>`;
  }).join('') + `</div><div style="font-size:10px;color:#64748b;margin-top:6px">
    Red is under the ${MIN_TARGET_PCT.toFixed(3)}% the engine requires. A round trip alone costs ${BREAK_EVEN_PCT.toFixed(3)}%.</div>`;
}

async function loadHistoric(){
  const d = await jget('/api/signals/history?days=365&before=7',{signals:[],counts:{}});
  const c = d.counts||{};
  document.getElementById('hist-cards').innerHTML =
      card('Archived signals', c.signals||0, 'older than 7 days')
    + card('Closed trades', c.trades||0, 'across all cycles')
    + card('Snapshots', c.snapshots||0, 'price + indicator')
    + card('Cycles', c.cycles||0, 'completed');
  const rows = d.signals||[];
  document.getElementById('hist-table').innerHTML = rows.length ? `
    <div class="scroll"><table class="tbl"><thead><tr>
      <th>Date</th><th>Symbol</th><th>Setup</th><th>Dir</th><th>Entry</th><th>Move</th><th>&times; cost</th><th>Conf</th><th>Outcome</th>
    </tr></thead><tbody>` + rows.map(x=>{
      const m=moveOf(x);
      return `<tr>
        <td class="sub">${new Date(x.timestamp).toLocaleString('en-IN',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'})}</td>
        <td><b>${esc(x.symbol)}</b></td>
        <td>${esc(CR_SIG_NAME[x.signal_type]||x.signal_type)}</td>
        <td class="${x.direction==='long'?'pos':'neg'}">${x.direction.toUpperCase()}</td>
        <td>${fmtPrice(x.current_price)}</td><td>${m.toFixed(3)}%</td>
        <td class="${m>=MIN_TARGET_PCT?'pos':'neg'}">${(m/BREAK_EVEN_PCT).toFixed(1)}&times;</td>
        <td>${x.confidence}%</td>
        <td class="sub">${esc(x.outcome||'pending')}</td></tr>`;
    }).join('') + '</tbody></table></div>'
    : '<div class="empty">Nothing older than 7 days yet</div>';
}

const TAB_META = {
  dashboard:{title:'Dashboard',       sub:'Last 7 days'},
  crypto:   {title:'Signals',         sub:'Live market and recent predictions'},
  paper:    {title:'Paper Trading',   sub:'Simulated only — never places a real order'},
  guard:    {title:'Session Guard',   sub:'Behavioural flags from your own trades'},
  accuracy: {title:'Accuracy',        sub:'Calibration, move size and setup performance'},
  historic: {title:'Historic Data',   sub:'Older than 7 days · read-only archive'},
  watchlist:{title:'Watchlist',       sub:'Symbols the collectors track'},
};

function switchTab(tab){
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
  document.querySelectorAll('.side-item').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
  document.querySelectorAll('.tab-content').forEach(c=>c.classList.toggle('active',c.id==='tab-'+tab));
  const m = TAB_META[tab];
  if(m){
    document.getElementById('page-title').textContent = m.title;
    document.getElementById('page-sub').textContent = m.sub;
  }
  // The hash keeps a reload on the same screen, which matters on a free
  // instance that restarts often.
  if(location.hash !== '#'+tab) history.replaceState(null,'','#'+tab);
  if(tab==='paper')     loadPaper();
  if(tab==='guard')     loadGuard();
  if(tab==='accuracy')  loadAccuracy();
  if(tab==='historic')  loadHistoric();
  if(tab==='watchlist') loadWatchlist();
  if(tab==='dashboard') loadDashboard();
  // Panels are no longer rebuilt while hidden, so arriving at one means its
  // data may be a refresh cycle old. Fetch what this tab actually needs.
  if(tab==='crypto') refresh();
}

function toggleMore(){
  const bar=document.getElementById('sidebar');
  const on=bar.classList.toggle('more-open');
  document.getElementById('more-scrim').classList.toggle('on',on);
}
// Picking a destination closes the sheet — leaving it open over the screen you
// just navigated to is the classic version of this bug.
document.addEventListener('click',e=>{
  const item=e.target.closest('.side-item');
  if(item && !item.classList.contains('side-more'))
    document.getElementById('sidebar').classList.remove('more-open'),
    document.getElementById('more-scrim').classList.remove('on');
});

function addCryptoSymbol2(){
  const el=document.getElementById('cr-add-input2');
  const v=(el.value||'').trim(); if(!v) return;
  el.value='';
  fetch('/api/crypto/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol:v})}).then(()=>loadWatchlist());
}

// ── CRYPTO ────────────────────────────────────────────────────────────────────
// Enough precision that entry, target and stop are always distinguishable.
// Rounding a $1,905 price to whole dollars made every signal render as
// "Entry $1,905  Target $1,905  Stop $1,905", hiding the real distances.
// A price distance, shown at the precision of the price it belongs to. Running
// a delta through fmtPrice alone gives "10.7500" for ten and three quarters.
function fmtDelta(d, ref){
  if(ref>=1000) return d.toFixed(2);
  if(ref>=1) return d.toFixed(4);
  return d.toFixed(6);
}
function fmtPrice(p){
  if(p == null || !isFinite(p)) return '—';
  if(p>=1000) return p.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  if(p>=1) return p.toFixed(4);
  return p.toFixed(6);
}
// Injected from the Python cost model at render time — see _COST_SNIPPET.
// Hardcoding these here is how the page and the engine drifted apart: the
// dashboard called a signal viable that the engine would have refused.
const BREAK_EVEN_PCT = __BREAK_EVEN_PCT__;
const MIN_TARGET_PCT = __MIN_TARGET_PCT__;
const PAPER_LEVERAGE = __PAPER_LEVERAGE__;
function renderCryptoCoins(coins){
  _crWatchlistSymbols = (coins||[]).map(c=>String(c.symbol).toUpperCase());
  renderCryptoSignalTabs();
  const el=document.getElementById('cr-coins');
  if(!coins.length){el.innerHTML='<div class="empty">Watchlist is empty — add a symbol above</div>';return;}
  el.innerHTML='<div class="cr-grid">'+coins.map(c=>{
    const up=c.change_24h_pct>=0;
    // A price of zero is missing data, not a price. Rendering $0.000000 the
    // same way as a real quote is how XAUUSDT looked live for hours.
    const hasData = c.price > 0;
    if(!hasData){
      return `<div class="cr-coin cr-coin-nodata">
        <div class="cr-coin-top"><span class="cr-coin-sym">${esc(c.symbol)}</span>
          <button class="cr-coin-remove" onclick="removeCryptoSymbol('${esc(c.symbol.toLowerCase())}')" title="Remove from watchlist">✕</button></div>
        <div class="cr-coin-price cr-nodata">no price feed</div>
        <div class="cr-coin-chg">Not carried by the spot ticker. Futures-only
          instruments like gold are fetched separately — check
          <span class="mono">/api/debug/coindcx?symbol=${esc(c.symbol.toLowerCase())}</span></div>
      </div>`;
    }
    return `<div class="cr-coin">
      <div class="cr-coin-top"><span class="cr-coin-sym">${esc(c.symbol)}</span>
        <button class="cr-coin-remove" onclick="removeCryptoSymbol('${esc(c.symbol.toLowerCase())}')" title="Remove from watchlist">✕</button></div>
      <div class="cr-coin-price">$${fmtPrice(c.price)}</div>
      <div class="cr-coin-chg ${up?'up':'down'}">${up?'▲':'▼'} ${Math.abs(c.change_24h_pct).toFixed(2)}% (24h)</div>
      <div class="cr-coin-stats"><span>RSI <b>${c.rsi_14.toFixed(0)}</b></span><span>ATR <b>${
        c.atr_pct==null?'—':c.atr_pct.toFixed(3)+'%'}</b></span><span>Vol <b>${
        c.volume_ratio>0?'×'+c.volume_ratio.toFixed(1):'n/a'}</b></span></div>
    </div>`;
  }).join('')+'</div>';
}
const CR_SIG_NAME={rsi_divergence:'Momentum Reversal',volume_spike:'Volume Surge',bollinger_squeeze:'Breakout Setup',sentiment_shift:'News Catalyst'};
const CR_SIG_PAGE_SIZE=8;
let _crSignalsAll=[];
let _crSignalFilter='ALL';
let _crSignalPage=0;

function renderCryptoSignals(signals){
  _crSignalsAll=signals||[];
  if(_crSignalFilter!=='ALL' && !_crSignalsAll.some(s=>s.symbol===_crSignalFilter)){
    _crSignalFilter='ALL';
  }
  renderCryptoSignalTabs();
  renderCryptoSignalsPage();
}

// Every watched symbol gets a tab, whether or not it has fired lately. Tabs
// built only from signals meant a quiet coin vanished from the filter — the
// one case where you most want to check whether anything fired.
let _crWatchlistSymbols = [];

function renderCryptoSignalTabs(){
  const el=document.getElementById('cr-sig-tabs');
  if(!el) return;
  const withSignals = new Set(_crSignalsAll.map(s=>s.symbol));
  const symbols=[...new Set([..._crWatchlistSymbols, ...withSignals])].sort();
  if(!symbols.length){el.innerHTML='';return;}
  const counts={};
  _crSignalsAll.forEach(s=>{counts[s.symbol]=(counts[s.symbol]||0)+1;});
  const tab=(t,label,n)=>
    `<button class="cr-sig-tab ${t===_crSignalFilter?'active':''}${n===0?' quiet':''}"
      onclick="setCryptoSignalFilter('${esc(t)}')">${esc(label)}${
      n===undefined?'':`<span class="cr-tab-n">${n}</span>`}</button>`;
  el.innerHTML = tab('ALL','All',_crSignalsAll.length)
    + symbols.map(sym=>tab(sym,sym,counts[sym]||0)).join('');
}

function setCryptoSignalFilter(sym){
  _crSignalFilter=sym;
  _crSignalPage=0;
  renderCryptoSignalTabs();
  renderCryptoSignalsPage();
}

function changeCryptoSignalPage(delta){
  _crSignalPage+=delta;
  renderCryptoSignalsPage();
}

function renderCryptoSignalsPage(){
  const el=document.getElementById('cr-signals');
  const pageEl=document.getElementById('cr-sig-pagination');
  const filtered=_crSignalFilter==='ALL'?_crSignalsAll:_crSignalsAll.filter(s=>s.symbol===_crSignalFilter);

  if(!filtered.length){
    el.innerHTML='<div class="empty">No crypto signals in the last 24 hours</div>';
    if(pageEl) pageEl.innerHTML='';
    return;
  }

  const totalPages=Math.max(1,Math.ceil(filtered.length/CR_SIG_PAGE_SIZE));
  _crSignalPage=Math.min(Math.max(0,_crSignalPage),totalPages-1);
  const start=_crSignalPage*CR_SIG_PAGE_SIZE;
  el.innerHTML=filtered.slice(start,start+CR_SIG_PAGE_SIZE).map(renderCryptoSignalCard).join('');

  if(pageEl){
    pageEl.innerHTML = totalPages<=1 ? '' : `
      <button class="cr-page-btn" ${_crSignalPage===0?'disabled':''} onclick="changeCryptoSignalPage(-1)">‹ Prev</button>
      <span class="cr-page-label">Page ${_crSignalPage+1} of ${totalPages}</span>
      <button class="cr-page-btn" ${_crSignalPage>=totalPages-1?'disabled':''} onclick="changeCryptoSignalPage(1)">Next ›</button>`;
  }
}

function fmtSignalTime(iso){
  if(!iso) return 'time unknown';
  const t = new Date(iso);
  if(isNaN(t)) return 'time unknown';
  const mins = Math.floor((Date.now() - t.getTime())/60000);
  let ago;
  if(mins < 1) ago = 'just now';
  else if(mins < 60) ago = mins + 'm ago';
  else if(mins < 1440) ago = Math.floor(mins/60) + 'h ' + (mins%60) + 'm ago';
  else ago = Math.floor(mins/1440) + 'd ago';
  const stamp = t.toLocaleString('en-IN',
    {day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:true});
  return stamp + ' IST · ' + ago;
}

function renderCryptoSignalCard(s){
  const name = CR_SIG_NAME[s.signal_type] || s.signal_type.replace(/_/g,' ');
  const long = s.direction === 'long';
  const entry = s.current_price, tp = s.target_price, sl = s.stop_loss;
  const move  = (tp && entry) ? Math.abs(tp - entry) / entry * 100 : 0;
  const risk  = (sl && entry) ? Math.abs(entry - sl) / entry * 100 : 0;
  const xcost = move / BREAK_EVEN_PCT;
  const viable = move >= MIN_TARGET_PCT;
  const roe = move * PAPER_LEVERAGE;

  // The track runs stop -> target, so it reads left-to-right the same way for
  // a long and a short even though price moves the opposite way.
  const pos = p => (tp === sl) ? 50
    : Math.max(0, Math.min(100, (p - sl) / (tp - sl) * 100));
  const at = pos(entry);

  const rr = risk > 0 ? (move / risk) : null;
  const cls = viable ? (long ? 'long' : 'short') : 'unviable';

  return `<div class="sig ${cls}">
    <div class="sig-head">
      <span class="sig-sym">${esc(s.symbol)}</span>
      <span class="sig-dir ${long?'long':'short'}">${long?'Long':'Short'} ${PAPER_LEVERAGE}x</span>
      <div style="flex-grow:1"></div>
      <div class="sig-profit ${viable?'':'muted'}">
        <b>${viable?'+':''}${roe.toFixed(1)}%</b><span>expected</span></div>
    </div>

    <div class="sig-meta">
      <span class="sig-setup">${esc(name)}</span>
      <span>&middot;</span><span class="sig-horizon" title="How long this move should take at this market's own pace — derived from the distance and the recent range, not a fixed window">~${esc(s.timeframe)} to target</span>
      <span>&middot;</span><span>${s.confidence}% confidence</span>
      <div style="flex-grow:1"></div>
      <span class="sig-when">${fmtSignalTime(s.timestamp)}</span>
    </div>

    <div class="sig-levels">
      <div><label>Stop loss</label><b class="neg">${fmtPrice(sl)}</b><span>−${risk.toFixed(2)}%</span></div>
      <div class="mid"><label>Entry</label><b>${fmtPrice(entry)}</b><span>LTP</span></div>
      <div class="right"><label>Take profit</label><b class="pos">${fmtPrice(tp)}</b><span>+${move.toFixed(2)}%</span></div>
    </div>

    <div class="sig-track">
      <span class="cap sl"></span>
      <span class="cap tp"></span>
      <span class="fill" style="width:${at}%"></span>
      <span class="now" style="left:${at}%"></span>
    </div>

    ${trailPlan(entry, sl, long)}

    <div class="sig-foot">
      <span class="pill ${viable?'ok':'bad'}">${xcost.toFixed(1)}× cost</span>
      ${rr?`<span class="pill">${rr.toFixed(2)} reward:risk</span>`:''}
      <span class="pill">${move.toFixed(3)}% move</span>
      <div style="flex-grow:1"></div>
      <span class="sig-act ${viable?(long?'long':'short'):'off'}">${
        viable ? (long?'Buy / Long':'Sell / Short') : 'Refused'}</span>
    </div>

    ${viable?'':`<div class="sig-warn">Target is ${move.toFixed(3)}% away against a
      ${BREAK_EVEN_PCT.toFixed(3)}% round trip. ${xcost <= 1
        ? 'It costs more to open and close than the move can win, so this loses money when it succeeds.'
        : `It would keep only ${(100-100/xcost).toFixed(0)}% of what it earns.`}
      The bot will not take a trade under ${MIN_TARGET_PCT.toFixed(3)}%.</div>`}
  </div>`;
}

// The exit, in prices, because the venue's TP/SL box takes prices. A 2x
// reward:risk and a trail are one decision: the target alone caps the winner
// that pays for the losers, and a trail behind a 1R target never arms.
function trailPlan(entry, stop, long){
  const risk = Math.abs(entry - stop);
  if(!entry || !risk) return '';
  const sign = long ? 1 : -1;
  const arm = entry + sign * 0.75 * risk;
  const be  = entry * (1 + sign * BREAK_EVEN_PCT/100);
  return `<div class="sig-trail">
    <span class="sig-trail-k">Trail</span>
    <span>at <b>${fmtPrice(arm)}</b> move the stop to <b>${fmtPrice(be)}</b>,
      then keep it <b>${fmtDelta(risk, entry)}</b> behind the best price</span>
  </div>`;
}

function renderCommodities(rows){
  const sec=document.getElementById('cr-commodities-section');
  const el=document.getElementById('cr-commodities');
  if(!rows||!rows.length){sec.style.display='none';return;}
  sec.style.display='';
  el.innerHTML='<div class="cr-commodity">'+rows.map(r=>`<div class="cr-comm-card">
    <div class="cr-comm-name">${esc(r.name)}</div><div class="cr-comm-price">$${fmtPrice(r.price)}</div>
  </div>`).join('')+'</div>';
}
async function addCryptoSymbol(){
  const inp=document.getElementById('cr-add-input');
  const sym=inp.value.trim().toLowerCase();
  if(!sym) return;
  await apiFetch('/api/crypto/watchlist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym})});
  inp.value='';
  refresh();
}
async function removeCryptoSymbol(sym){
  await apiFetch('/api/crypto/watchlist/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym})});
  refresh();
}

// ── MAIN ──────────────────────────────────────────────────────────────────────
// Fetch JSON that never rejects — a single failing endpoint must not blank the whole dashboard.
function jget(url,fallback){return fetch(url).then(r=>r.ok?r.json():fallback).catch(()=>fallback);}
function setText(id, value){
  const el = document.getElementById(id);
  if(el) el.textContent = value;
  return !!el;
}
function setHTML(id, value){
  const el = document.getElementById(id);
  if(el) el.innerHTML = value;
  return !!el;
}

// Which tabs need which payload. Rebuilding a panel nobody is looking at
// still costs a full style recalc, layout and paint — every thirty seconds,
// for markup that is display:none. The crypto cards alone were being torn
// down and rebuilt while the Dashboard was on screen, which is most of the
// jank this page had.
const TAB_NEEDS = {
  dashboard: ['coins','signals'],
  crypto:    ['coins','signals','commodities'],
  watchlist: ['coins'],
  paper:     ['paper'],
};
function _activeTab(){
  const el = document.querySelector('.tab-content.active');
  return el ? el.id.replace(/^tab-/, '') : 'dashboard';
}

async function refresh(){
  // Nothing on a backgrounded tab is worth a request. The browser throttles
  // the timer anyway; this stops the work the timer would still queue up.
  if(document.hidden){ setText('refresh-label', 'Paused'); return; }
  try{
    const need = TAB_NEEDS[_activeTab()] || [];
    const status = await jget('/api/status',{});
    // The sidebar footer is where uptime lives.
    setText('side-uptime', 'up ' + fmtUptime(status.uptime_seconds));
    setText('side-status', 'Running');

    if(need.includes('coins') || need.includes('signals') || need.includes('commodities')){
      const [crCoins,crSignals,crCommodities]=await Promise.all([
        need.includes('coins')       ? jget('/api/crypto/coins',[])   : Promise.resolve(null),
        need.includes('signals')     ? jget('/api/crypto/signals',[]) : Promise.resolve(null),
        need.includes('commodities') ? jget('/api/commodities',[])    : Promise.resolve(null),
      ]);
      if(crCoins){ setText('stat-crypto-coins', crCoins.length); renderCryptoCoins(crCoins); }
      if(crSignals){ setText('stat-crypto-signals', crSignals.length); renderCryptoSignals(crSignals); }
      if(crCommodities) renderCommodities(crCommodities);
    }

    if(need.includes('paper')) await loadPaper();

    setText('last-updated', 'Updated: '+new Date().toLocaleTimeString('en-IN',_IST)+' IST');
    setText('refresh-label', 'Next in 30s');
  }catch(e){
    console.error('refresh error:', e);
    setText('refresh-label', 'Error — retrying…');
  }
}

// Coming back to a tab that was paused should show current data at once,
// not whatever was on screen when it was hidden.
document.addEventListener('visibilitychange', () => { if(!document.hidden) refresh(); });

switchTab((location.hash||'#dashboard').slice(1));
refresh();
setInterval(refresh,30000);
</script>
</body>
</html>"""


_DATA_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Database Dump — Crypto Signal Engine</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;padding-bottom:40px}
header{background:#1e293b;border-bottom:1px solid #334155;padding:14px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
header h1{font-size:17px;font-weight:700;color:#f1f5f9;display:flex;align-items:center;gap:8px}
header a.nav-btn{background:#0ea5e9;color:#fff;font-size:13px;font-weight:600;padding:6px 14px;border-radius:8px;text-decoration:none;white-space:nowrap}
header a.nav-btn:hover{background:#0284c7}
.controls{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:13px;color:#94a3b8}
.controls input{width:70px;background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:6px;padding:4px 8px}
.controls button{background:#0ea5e9;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-weight:600;cursor:pointer}
.controls button:hover{background:#0284c7}
.toc{padding:12px 20px;display:flex;flex-wrap:wrap;gap:8px}
.toc a{font-size:12px;background:#1e293b;border:1px solid #334155;color:#cbd5e1;padding:4px 10px;border-radius:9999px;text-decoration:none}
.toc a:hover{border-color:#0ea5e9;color:#fff}
.toc a b{color:#38bdf8}
section{padding:8px 20px 20px}
.tbl-head{display:flex;align-items:baseline;gap:10px;margin:18px 0 8px;border-bottom:1px solid #334155;padding-bottom:6px}
.tbl-head h2{font-size:15px;font-weight:700;color:#f1f5f9}
.tbl-head .count{font-size:12px;color:#64748b}
.tbl-head .count b{color:#22c55e}
.scroll{overflow-x:auto;border:1px solid #334155;border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}
th,td{border:1px solid #1e293b;padding:5px 9px;text-align:left;max-width:360px;overflow:hidden;text-overflow:ellipsis}
th{background:#1e293b;color:#94a3b8;position:sticky;top:0;font-weight:600}
tr:nth-child(even) td{background:#172033}
td.null{color:#475569;font-style:italic}
.empty{color:#475569;font-size:13px;padding:14px 0}
.err{color:#f87171;font-size:12px;padding:8px 0}
.note{color:#64748b;font-size:12px;padding:0 20px}
#status{color:#94a3b8;font-size:12px}

@media(max-width:640px){
  html{-webkit-text-size-adjust:100%}
  body{padding:12px}
  table{font-size:12px}
  th,td{padding:7px 8px}
  input,select,textarea,button{min-height:40px;font-size:16px}
  .grid,.cards{grid-template-columns:1fr !important}
  pre{font-size:11px;overflow-x:auto}
}
</style>
</head>
<body>
<header>
  <h1>🗄️ Database Dump</h1>
  <a class="nav-btn" href="/">← Home</a>
  <a class="nav-btn" href="/settings">⚙️ Settings</a>
  <div class="controls">
    <span id="status">loading…</span>
    <label>rows/table
      <input id="limit" type="number" min="1" max="2000" value="100">
    </label>
    <button onclick="load()">Reload</button>
  </div>
</header>
<div class="toc" id="toc"></div>
<div class="note">Newest rows first (by primary key). Increase rows/table to dump more — capped at 2000 per table.</div>
<div id="tables"></div>
<script>
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function cell(v){
  if(v===null||v===undefined) return '<td class="null">NULL</td>';
  return '<td title="'+esc(v)+'">'+esc(v)+'</td>';
}
function renderTable(t){
  const head='<div class="tbl-head" id="t_'+esc(t.name)+'">'
    +'<h2>'+esc(t.name)+'</h2>'
    +'<span class="count">showing <b>'+t.shown+'</b> of '+t.total.toLocaleString()+' rows</span></div>';
  if(t.error) return head+'<div class="err">error: '+esc(t.error)+'</div>';
  if(!t.rows.length) return head+'<div class="empty">— empty —</div>';
  let h='<div class="scroll"><table><thead><tr>';
  for(const c of t.columns) h+='<th>'+esc(c)+'</th>';
  h+='</tr></thead><tbody>';
  for(const row of t.rows){
    h+='<tr>';
    for(const v of row) h+=cell(v);
    h+='</tr>';
  }
  h+='</tbody></table></div>';
  return head+h;
}
async function load(){
  const lim=Math.max(1,Math.min(2000,parseInt(document.getElementById('limit').value)||100));
  document.getElementById('status').textContent='loading…';
  try{
    const data=await fetch('/api/tables?limit='+lim).then(r=>r.json());
    const tables=data.tables||[];
    document.getElementById('toc').innerHTML=tables.map(t=>
      '<a href="#t_'+esc(t.name)+'">'+esc(t.name)+' <b>'+t.total.toLocaleString()+'</b></a>').join('');
    document.getElementById('tables').innerHTML=
      tables.map(t=>'<section>'+renderTable(t)+'</section>').join('');
    const totRows=tables.reduce((a,t)=>a+t.total,0);
    document.getElementById('status').textContent=
      tables.length+' tables · '+totRows.toLocaleString()+' rows total';
  }catch(e){
    document.getElementById('status').textContent='Error: '+e;
    document.getElementById('tables').innerHTML='<div class="err" style="padding:20px">Failed to load: '+esc(e)+'</div>';
  }
}
load();
</script>
</body>
</html>"""


async def _api_tables(runner, request: web.Request) -> web.Response:
    """Dump every table in the database (reflected, so it covers all tables).

    Query params:
      limit — rows per table (default 100, max 2000)
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    from storage.database import engine

    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 2000))
    except ValueError:
        limit = 100

    out: dict = {"generated_at": _iso(datetime.now(UTC)), "limit": limit,
                 "tables": []}

    async with engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: sa_inspect(sync_conn).get_table_names()
        )
        pk_map = await conn.run_sync(
            lambda sync_conn: {
                t: sa_inspect(sync_conn).get_pk_constraint(t).get("constrained_columns", [])
                for t in table_names
            }
        )

        for name in sorted(table_names):
            try:
                total = (await conn.execute(
                    text(f'SELECT COUNT(*) FROM "{name}"'))).scalar() or 0
            except Exception:
                total = 0

            # Newest-first when there's a single-column primary key, else natural order.
            pks = pk_map.get(name) or []
            order = f' ORDER BY "{pks[0]}" DESC' if len(pks) == 1 else ""
            cols: list[str] = []
            rows: list[list] = []
            error = None
            try:
                result = await conn.execute(
                    text(f'SELECT * FROM "{name}"{order} LIMIT :lim'), {"lim": limit})
                cols = list(result.keys())
                rows = [list(r) for r in result.fetchall()]
            except Exception as exc:
                error = str(exc)

            out["tables"].append({
                "name": name,
                "columns": cols,
                "rows": rows,
                "shown": len(rows),
                "total": total,
                "error": error,
            })

    return web.Response(text=json.dumps(out, default=str),
                        content_type="application/json")


async def _data_page(request: web.Request) -> web.Response:
    return web.Response(text=_DATA_HTML, content_type="text/html")


async def _settings_page(request: web.Request) -> web.Response:
    return web.Response(text=_SETTINGS_HTML, content_type="text/html")


async def _api_auth_verify(runner, request: web.Request) -> web.Response:
    """
    POST /api/auth/verify — does this token work?

    The whole endpoint. Used exactly once by a new client (the iOS app
    entering a token for the first time, gated behind Face ID before it is
    written to Keychain) to confirm the token is right before trusting it for
    everything else. It carries a far tighter rate limit than the rest of the
    API (see scheduler/security.py) precisely because its job is accepting
    attempts at the shared secret.
    """
    from scheduler.security import check_bearer_auth
    denied = check_bearer_auth(request, _SETTINGS.api_auth_token)
    if denied is not None:
        return denied
    return web.json_response({"ok": True})


async def _verify_admin_session(request: web.Request) -> bool:
    """Check X-Settings-Token header, Bearer token, or query param against DB."""
    token = request.headers.get("X-Settings-Token") or ""
    if not token:
        auth_hdr = request.headers.get("Authorization") or ""
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:].strip()
    if not token:
        token = request.query.get("token") or ""
    if not token:
        return False
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    try:
        async with AsyncSessionFactory() as session:
            return await Repository(session).validate_session_token(token)
    except Exception:
        return False


async def _api_settings_auth_login(runner, request: web.Request) -> web.Response:
    try:
        body = await request.json()
        password = str(body.get("password") or "")
        if not password:
            return web.json_response({"ok": False, "error": "Password required"}, status=400)
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            ok, token = await repo.verify_admin_password(password)
            if not ok or not token:
                return web.json_response({"ok": False, "error": "Invalid password"}, status=401)
            return web.json_response({"ok": True, "token": token})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def _api_settings_auth_status(runner, request: web.Request) -> web.Response:
    ok = await _verify_admin_session(request)
    return web.json_response({"authenticated": ok})


async def _api_settings_auth_logout(runner, request: web.Request) -> web.Response:
    token = request.headers.get("X-Settings-Token") or ""
    if not token:
        auth_hdr = request.headers.get("Authorization") or ""
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:].strip()
    if token:
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        try:
            async with AsyncSessionFactory() as session:
                await Repository(session).invalidate_session_token(token)
        except Exception:
            pass
    return web.json_response({"ok": True})


async def _api_paper_config_get(runner, request: web.Request) -> web.Response:
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    async with AsyncSessionFactory() as session:
        cfg = await Repository(session).get_paper_config()
        return web.json_response({
            "enabled": cfg.enabled,
            "starting_wallet": cfg.starting_wallet,
            "target_wallet": cfg.target_wallet,
            "leverage": cfg.leverage,
            "stop_pct_of_margin": cfg.stop_pct_of_margin,
            "reward_risk": cfg.reward_risk,
            "min_confidence": cfg.min_confidence,
            "max_concurrent": cfg.max_concurrent,
            "max_hold_minutes": cfg.max_hold_minutes,
            "scaled_sizing": cfg.scaled_sizing,
            "trailing_enabled": cfg.trailing_enabled,
            "scaled_leverage": cfg.scaled_leverage,
            "ladder_enabled": cfg.ladder_enabled,
            "ladder_tight": cfg.ladder_tight,
            "max_leverage": cfg.max_leverage,
            "usdt_inr": cfg.usdt_inr,
            "alert_telegram": cfg.alert_telegram,
        })


async def _api_paper_config_post(runner, request: web.Request) -> web.Response:
    is_admin = await _verify_admin_session(request)
    if not is_admin:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            cfg = await repo.update_paper_config(
                enabled=bool(body["enabled"]) if "enabled" in body else None,
                starting_wallet=float(body["starting_wallet"]) if "starting_wallet" in body else None,
                target_wallet=float(body["target_wallet"]) if "target_wallet" in body else None,
                leverage=float(body["leverage"]) if "leverage" in body else None,
                stop_pct_of_margin=float(body["stop_pct_of_margin"]) if "stop_pct_of_margin" in body else None,
                reward_risk=float(body["reward_risk"]) if "reward_risk" in body else None,
                min_confidence=float(body["min_confidence"]) if "min_confidence" in body else None,
                max_concurrent=int(body["max_concurrent"]) if "max_concurrent" in body else None,
                max_hold_minutes=int(body["max_hold_minutes"]) if "max_hold_minutes" in body else None,
                scaled_sizing=bool(body["scaled_sizing"]) if "scaled_sizing" in body else None,
                trailing_enabled=bool(body["trailing_enabled"]) if "trailing_enabled" in body else None,
                scaled_leverage=bool(body["scaled_leverage"]) if "scaled_leverage" in body else None,
                ladder_enabled=bool(body["ladder_enabled"]) if "ladder_enabled" in body else None,
                ladder_tight=bool(body["ladder_tight"]) if "ladder_tight" in body else None,
                max_leverage=float(body["max_leverage"]) if "max_leverage" in body else None,
                usdt_inr=float(body["usdt_inr"]) if "usdt_inr" in body else None,
                alert_telegram=bool(body["alert_telegram"]) if "alert_telegram" in body else None,
            )
            return web.json_response({"ok": True, "enabled": cfg.enabled, "starting_wallet": cfg.starting_wallet})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def _api_strategy_config_get(runner, request: web.Request) -> web.Response:
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    async with AsyncSessionFactory() as session:
        cfg = await Repository(session).get_strategy_config()
        return web.json_response({
            "crypto_min_confidence": cfg.crypto_min_confidence,
            "high_conviction_only": cfg.high_conviction_only,
            "crypto_volume_spike_enabled": cfg.crypto_volume_spike_enabled,
            "crypto_htf_filter_enabled": cfg.crypto_htf_filter_enabled,
            "binance_klines_enabled": cfg.binance_klines_enabled,
            "binance_oi_enabled": cfg.binance_oi_enabled,
            "orderflow_enabled": cfg.orderflow_enabled,
            "groq_signal_review_enabled": cfg.groq_signal_review_enabled,
            "groq_model": cfg.groq_model,
            "bank_size": cfg.bank_size,
            "min_confidence": cfg.min_confidence,
        })


async def _api_strategy_config_post(runner, request: web.Request) -> web.Response:
    is_admin = await _verify_admin_session(request)
    if not is_admin:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            cfg = await repo.update_strategy_config(
                crypto_min_confidence=float(body["crypto_min_confidence"]) if "crypto_min_confidence" in body else None,
                high_conviction_only=bool(body["high_conviction_only"]) if "high_conviction_only" in body else None,
                crypto_volume_spike_enabled=bool(body["crypto_volume_spike_enabled"]) if "crypto_volume_spike_enabled" in body else None,
                crypto_htf_filter_enabled=bool(body["crypto_htf_filter_enabled"]) if "crypto_htf_filter_enabled" in body else None,
                binance_klines_enabled=bool(body["binance_klines_enabled"]) if "binance_klines_enabled" in body else None,
                binance_oi_enabled=bool(body["binance_oi_enabled"]) if "binance_oi_enabled" in body else None,
                orderflow_enabled=bool(body["orderflow_enabled"]) if "orderflow_enabled" in body else None,
                groq_signal_review_enabled=bool(body["groq_signal_review_enabled"]) if "groq_signal_review_enabled" in body else None,
                groq_model=str(body["groq_model"]) if "groq_model" in body else None,
                bank_size=float(body["bank_size"]) if "bank_size" in body else None,
                min_confidence=float(body["min_confidence"]) if "min_confidence" in body else None,
            )
            return web.json_response({"ok": True, "groq_model": cfg.groq_model})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def _api_collector_toggle(runner, request: web.Request) -> web.Response:
    """POST /api/settings/toggle  body: {"collector": "coindcx", "enabled": true}"""
    from scheduler.security import check_bearer_auth
    is_admin = await _verify_admin_session(request)
    if not is_admin:
        denied = check_bearer_auth(request, _SETTINGS.api_auth_token)
        if denied is not None:
            return denied
    try:
        body = await request.json()
        collector = str(body.get("collector", ""))
        enabled = bool(body.get("enabled", True))
        if collector not in runner.collector_enabled:
            return web.Response(
                text=json.dumps({"error": f"unknown collector: {collector}"}),
                content_type="application/json", status=400,
            )
        runner.collector_enabled[collector] = enabled
        import structlog as _slog
        _slog.get_logger().info("collector_toggled", collector=collector, enabled=enabled)
        return web.Response(
            text=json.dumps({"collector": collector, "enabled": enabled, "ok": True}),
            content_type="application/json",
        )
    except Exception as exc:
        return web.Response(
            text=json.dumps({"error": str(exc)}),
            content_type="application/json", status=500,
        )


async def _api_collector_states(runner, request: web.Request) -> web.Response:
    """GET /api/settings — returns collector enabled/disabled states with quota info."""
    try:
        from config.settings import settings as _settings
        states = {}
        for name, enabled in runner.collector_enabled.items():
            states[name] = {"enabled": enabled}

        # Enrich with quota / key info — safely access with getattr
        if hasattr(runner, "coindcx"):
            states["coindcx"].update({
                "consecutive_failures": runner.coindcx._consecutive_failures,
                "matched_symbols": len(runner.coindcx.last_matched_symbols),
                "watchlist_size": len(await runner.crypto_store.get_symbols()),
            })
        if hasattr(runner, "coingecko"):
            states["coingecko"].update({
                "consecutive_failures": runner.coingecko._consecutive_failures,
                "watchlist_size": len(await runner.crypto_store.get_symbols()),
            })
        if hasattr(runner, "binance_ws"):
            states["binance_ws"].update({
                "connected": runner.binance_ws._running and runner.binance_ws._consecutive_failures == 0,
                "messages_received": runner.binance_ws._total_messages_received,
            })
        if hasattr(runner, "twelvedata_ws"):
            states["twelvedata_ws"].update({"key_set": bool(_settings.twelvedata_api_key)})

        return web.Response(text=json.dumps(states), content_type="application/json")
    except Exception as e:
        import traceback
        return web.Response(
            text=json.dumps({"error": str(e), "detail": traceback.format_exc()[-200:]}),
            content_type="application/json",
            status=500,
        )


_SETTINGS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>System Settings — Signal Engine</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh}
.topbar{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:100}
.topbar a{color:#58a6ff;text-decoration:none;font-size:13.5px;padding:6px 12px;border-radius:6px;border:1px solid #30363d;transition:background .2s}
.topbar a:hover{background:#21262d}
.topbar h1{font-size:16px;font-weight:600;color:#e6edf3;margin-left:4px}
.btn-lock{margin-left:auto;background:#21262d;border:1px solid #30363d;color:#f85149;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;transition:all .2s;display:flex;align-items:center;gap:6px}
.btn-lock:hover{background:#3d0a0a;border-color:#f85149}
.container{max-width:820px;margin:32px auto;padding:0 16px;padding-bottom:60px}
h2{font-size:19px;font-weight:700;margin-bottom:6px;display:flex;align-items:center;gap:8px}
.subtitle{color:#8b949e;font-size:13px;margin-bottom:22px;line-height:1.5}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:22px 24px;margin-bottom:24px;transition:border-color .2s}
.card.active{border-color:#238636}
.card.paused{border-color:#f85149;opacity:.85}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.card-title{display:flex;align-items:center;gap:10px}
.card-name{font-size:15.5px;font-weight:600}
.badge{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:600}
.badge-green{background:#0d4429;color:#3fb950}
.badge-red{background:#3d0a0a;color:#f85149}
.badge-yellow{background:#3d2b00;color:#e3b341}
.card-meta{color:#8b949e;font-size:13px;line-height:1.6}
.meta-row{display:flex;justify-content:space-between;margin-top:6px}
.meta-label{color:#8b949e}
.meta-value{color:#e6edf3;font-weight:500}
.quota-bar{height:6px;background:#21262d;border-radius:3px;margin-top:10px;overflow:hidden}
.quota-fill{height:100%;border-radius:3px;transition:width .4s}
.quota-fill.safe{background:#238636}
.quota-fill.warn{background:#e3b341}
.quota-fill.danger{background:#f85149}
/* Form Controls */
.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px;margin-top:16px}
.form-group{display:flex;flex-direction:column;gap:6px}
.form-group label{font-size:11.5px;color:#8b949e;text-transform:uppercase;font-weight:700;letter-spacing:0.5px}
.form-group input,.form-group select{background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;padding:10px 12px;font-size:13.5px;outline:none;transition:border-color .2s}
.form-group input:focus,.form-group select:focus{border-color:#58a6ff}
.toggle-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-top:20px;padding-top:16px;border-top:1px solid #21262d}
.toggle-item{display:flex;align-items:center;justify-content:space-between;background:#0d1117;padding:12px 14px;border-radius:8px;border:1px solid #21262d}
.toggle-item span{font-size:13px;font-weight:500;color:#e6edf3}
/* Toggle Switch */
.toggle-wrap{display:flex;align-items:center;gap:8px}
.toggle-label{font-size:12px;color:#8b949e;min-width:36px;text-align:right}
.toggle{position:relative;width:44px;height:24px;cursor:pointer}
.toggle input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:#30363d;border-radius:24px;transition:.3s}
.slider:before{content:'';position:absolute;width:18px;height:18px;left:3px;bottom:3px;background:#e6edf3;border-radius:50%;transition:.3s}
input:checked+.slider{background:#238636}
input:checked+.slider:before{transform:translateX(20px)}
.save-btn{background:#238636;border:none;color:#fff;font-size:14px;font-weight:600;padding:11px 24px;border-radius:8px;cursor:pointer;margin-top:20px;width:100%;transition:background .2s}
.save-btn:hover{background:#2ea043}
.toast{position:fixed;bottom:24px;right:24px;background:#238636;color:#fff;padding:12px 20px;border-radius:8px;font-size:14px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:9999}
.toast.show{opacity:1}
.toast.err{background:#f85149}
.theme-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:32px}
.theme-sw{background:#161b22;border:1px solid #30363d;color:#c9d1d9;padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:6px;transition:all .2s}
.theme-sw:hover{border-color:#58a6ff;color:#fff}
.theme-sw.on{border-color:#58a6ff;background:#1f242c;color:#fff}
.theme-sw .sw{width:10px;height:10px;border-radius:50%;display:inline-block}
/* Lock Overlay */
.lock-overlay{position:fixed;inset:0;background:rgba(13,17,23,0.96);backdrop-filter:blur(10px);display:flex;align-items:center;justify-content:center;z-index:10000}
.lock-box{background:#161b22;border:1px solid #30363d;border-radius:16px;padding:36px 32px;max-width:380px;width:90%;text-align:center;box-shadow:0 20px 40px rgba(0,0,0,0.6)}
.lock-box input{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;padding:12px 14px;font-size:16px;margin-top:18px;text-align:center;letter-spacing:3px;outline:none}
.lock-box input:focus{border-color:#58a6ff}
.lock-err{color:#f85149;font-size:13px;margin-top:12px;display:none}
@media(max-width:640px){
  .form-grid{grid-template-columns:1fr !important}
  .toggle-grid{grid-template-columns:1fr !important}
  .container{padding:12px}
}
</style>
</head>
<body>

<div id="lock-screen" class="lock-overlay">
  <div class="lock-box">
    <div style="font-size:44px;margin-bottom:12px">🔒</div>
    <h2 style="justify-content:center;font-size:21px">Settings Locked</h2>
    <p style="color:#8b949e;font-size:13px;line-height:1.5;margin-top:6px">Enter your administrator password to unlock and manage configuration.</p>
    <input type="password" id="admin-pwd" placeholder="Enter password" autocomplete="off" onkeydown="if(event.key==='Enter')login()">
    <div id="login-err" class="lock-err"></div>
    <button class="save-btn" style="margin-top:18px" onclick="login()">Unlock Settings</button>
  </div>
</div>

<div class="topbar">
  <a href="/">← Dashboard</a>
  <a href="/data">📊 History</a>
  <h1>⚙️ System Settings</h1>
  <button class="btn-lock" onclick="logout()"><span>🔒</span> Lock & Logout</button>
</div>

<div class="container" id="settings-content" style="display:none">
  <h2>🎨 Site Theme</h2>
  <p class="subtitle">Applies instantly across all pages — saved in this browser.</p>
  <div class="theme-row">
    <button class="theme-sw" data-t="amber" onclick="setSiteTheme('amber')"><span class="sw" style="background:#f59e0b"></span>Amber Terminal</button>
    <button class="theme-sw" data-t="carbon" onclick="setSiteTheme('carbon')"><span class="sw" style="background:#a3e635"></span>Carbon Lime</button>
    <button class="theme-sw" data-t="crimson" onclick="setSiteTheme('crimson')"><span class="sw" style="background:#fb7185"></span>Crimson</button>
    <button class="theme-sw" data-t="navy" onclick="setSiteTheme('navy')"><span class="sw" style="background:#0ea5e9"></span>Deep Navy</button>
    <button class="theme-sw" data-t="light" onclick="setSiteTheme('light')"><span class="sw" style="background:#eef2f7"></span>Polar White</button>
    <button class="theme-sw" data-t="violet" onclick="setSiteTheme('violet')"><span class="sw" style="background:#6d28d9"></span>Violet Night</button>
    <button class="theme-sw" data-t="emerald" onclick="setSiteTheme('emerald')"><span class="sw" style="background:#15803d"></span>Emerald Court</button>
  </div>
  <script>setSiteTheme(localStorage.getItem('site_theme')||'amber');</script>

  <h2>📈 Paper Trading Configuration</h2>
  <p class="subtitle">Stored directly in database — updates apply live to simulator cycle without redeployment.</p>
  <div class="card">
    <div style="background: rgba(30, 41, 59, 0.7); border: 1px solid #334155; padding: 14px 18px; border-radius: 8px; margin-bottom: 18px; display: flex; align-items: center; justify-content: space-between;">
      <div>
        <div style="font-weight: 600; font-size: 15px; color: #f8fafc;">🚀 Paper Trading Simulator Master Switch</div>
        <div style="font-size: 12px; color: #94a3b8;">When active, signals matching conviction thresholds will open paper positions and simulate live trades.</div>
      </div>
      <label class="toggle"><input type="checkbox" id="p-enabled"><span class="slider"></span></label>
    </div>
    <div class="form-grid">
      <div class="form-group">
        <label>Starting Wallet (₹)</label>
        <input type="number" id="p-starting-wallet" step="100">
      </div>
      <div class="form-group">
        <label>Target Wallet (₹)</label>
        <input type="number" id="p-target-wallet" step="500">
      </div>
      <div class="form-group">
        <label>USDT / INR Rate (₹)</label>
        <input type="number" id="p-usdt-inr" step="0.1">
      </div>
      <div class="form-group">
        <label>Base Leverage (x)</label>
        <input type="number" id="p-leverage" step="1" min="1" max="100">
      </div>
      <div class="form-group">
        <label>Max Leverage (x)</label>
        <input type="number" id="p-max-leverage" step="1" min="1" max="100">
      </div>
      <div class="form-group">
        <label>Stop Loss (% of Margin)</label>
        <input type="number" id="p-stop-pct" step="0.01" min="0.01" max="1.0">
      </div>
      <div class="form-group">
        <label>Reward / Risk Ratio</label>
        <input type="number" id="p-reward-risk" step="0.1" min="0.5">
      </div>
      <div class="form-group">
        <label>Min Confidence (0.50 - 0.95)</label>
        <input type="number" id="p-min-confidence" step="0.01" min="0.50" max="0.99">
      </div>
      <div class="form-group">
        <label>Max Concurrent Trades</label>
        <input type="number" id="p-max-concurrent" step="1" min="1" max="20">
      </div>
      <div class="form-group">
        <label>Max Hold Duration (mins)</label>
        <input type="number" id="p-max-hold" step="10" min="10">
      </div>
    </div>

    <div class="toggle-grid">
      <div class="toggle-item">
        <span>Scaled Position Sizing (Tiering)</span>
        <label class="toggle"><input type="checkbox" id="p-scaled-sizing"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Trailing Stop Loss</span>
        <label class="toggle"><input type="checkbox" id="p-trailing"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Dynamic Conviction Leverage</span>
        <label class="toggle"><input type="checkbox" id="p-scaled-leverage"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Profit Ladder Ratchet</span>
        <label class="toggle"><input type="checkbox" id="p-ladder"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Tight Ladder Mode</span>
        <label class="toggle"><input type="checkbox" id="p-ladder-tight"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Telegram Fill & Exit Alerts</span>
        <label class="toggle"><input type="checkbox" id="p-telegram"><span class="slider"></span></label>
      </div>
    </div>
    <button class="save-btn" onclick="savePaperConfig()">💾 Save Paper Trading Configuration</button>
  </div>

  <h2>🤖 Strategy, AI Review & Market Data</h2>
  <p class="subtitle">Core engine conviction thresholds, Groq pre-signal review, and institutional orderflow feeds.</p>
  <div class="card">
    <div class="form-grid">
      <div class="form-group">
        <label>Groq AI Model Selection</label>
        <select id="s-groq-model" style="background:#0f172a; color:#f8fafc; border:1px solid #334155; padding:8px 10px; border-radius:6px; width:100%; font-size:13px;">
          <option value="qwen/qwen3.8-27b">Qwen 3.8 27B (Recommended - Fast & Analytical)</option>
          <option value="llama-3.3-70b-versatile">Llama 3.3 70B Versatile (Deep Reasoning)</option>
          <option value="llama-3.1-8b-instant">Llama 3.1 8B Instant (Ultra-fast)</option>
          <option value="mixtral-8x7b-32768">Mixtral 8x7B (High Context)</option>
          <option value="deepseek-r1-distill-llama-70b">DeepSeek R1 Distill Llama 70B (Math & Logic)</option>
        </select>
      </div>
      <div class="form-group">
        <label>Crypto Min Confidence (0.50 - 0.95)</label>
        <input type="number" id="s-min-confidence" step="0.01" min="0.50" max="0.99">
      </div>
      <div class="form-group">
        <label>Scalp Bank Size (₹)</label>
        <input type="number" id="s-bank-size" step="500">
      </div>
    </div>

    <div class="toggle-grid">
      <div class="toggle-item">
        <span>Groq Pre-Signal AI Review</span>
        <label class="toggle"><input type="checkbox" id="s-groq-review"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>High Conviction Mode (5x Edge)</span>
        <label class="toggle"><input type="checkbox" id="s-high-conviction"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Binance Futures Open Interest (OI)</span>
        <label class="toggle"><input type="checkbox" id="s-binance-oi"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Order Flow & CVD Absorption</span>
        <label class="toggle"><input type="checkbox" id="s-orderflow"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Binance 1m True OHLC Klines</span>
        <label class="toggle"><input type="checkbox" id="s-binance-klines"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Volume Spike Confirmation</span>
        <label class="toggle"><input type="checkbox" id="s-volume-spike"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>HTF Trend Filter</span>
        <label class="toggle"><input type="checkbox" id="s-htf-filter"><span class="slider"></span></label>
      </div>
    </div>
    <button class="save-btn" onclick="saveStrategyConfig()">💾 Save Strategy & AI Configuration</button>
  </div>

  <h2>📡 Collector Data Sources</h2>
  <p class="subtitle">Toggle individual third-party feeds on/off to conserve external quota.</p>
  <div id="cards">Loading collectors...</div>
</div>

<div class="toast" id="toast"></div>

<script>
const SOURCES = [
  {id:'coindcx',name:'CoinDCX',icon:'🪙',desc:'Crypto price polling — preferred source, exact exchange prices',quota_total:null},
  {id:'coingecko',name:'CoinGecko',icon:'🦎',desc:'Crypto price polling — fallback for unlisted symbols',quota_total:null},
  {id:'binance_ws',name:'Binance WebSocket',icon:'🚫',desc:'Real-time crypto streaming (OFF by default)',quota_total:null},
  {id:'twelvedata_ws',name:'Twelve Data',icon:'🥇',desc:'Gold / Silver / Crude Oil live prices',quota_total:null},
];

let states = {};

async function apiFetch(url, opts) {
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers);
  const token = sessionStorage.getItem('settings_token');
  if (token) {
    opts.headers['X-Settings-Token'] = token;
    opts.headers['Authorization'] = 'Bearer ' + token;
  }
  const res = await fetch(url, opts);
  if (res.status === 401) {
    sessionStorage.removeItem('settings_token');
    showLockScreen('Session expired or unauthorized. Please re-enter password.');
  }
  return res;
}

function showLockScreen(errMsg) {
  document.getElementById('lock-screen').style.display = 'flex';
  document.getElementById('settings-content').style.display = 'none';
  const errEl = document.getElementById('login-err');
  if (errMsg) {
    errEl.textContent = errMsg;
    errEl.style.display = 'block';
  } else {
    errEl.style.display = 'none';
  }
  const pwdInput = document.getElementById('admin-pwd');
  pwdInput.value = '';
  setTimeout(() => pwdInput.focus(), 100);
}

function unlockScreen() {
  document.getElementById('lock-screen').style.display = 'none';
  document.getElementById('settings-content').style.display = 'block';
  loadAll();
}

async function login() {
  const pwd = document.getElementById('admin-pwd').value.trim();
  const errEl = document.getElementById('login-err');
  if (!pwd) {
    errEl.textContent = 'Please enter password';
    errEl.style.display = 'block';
    return;
  }
  try {
    const res = await fetch('/api/settings/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: pwd}),
    });
    const data = await res.json();
    if (data.ok && data.token) {
      sessionStorage.setItem('settings_token', data.token);
      unlockScreen();
      showToast('Settings unlocked ✓');
    } else {
      errEl.textContent = data.error || 'Invalid password';
      errEl.style.display = 'block';
    }
  } catch(e) {
    errEl.textContent = 'Network connection failed';
    errEl.style.display = 'block';
  }
}

async function logout() {
  try {
    await apiFetch('/api/settings/auth/logout', {method: 'POST'});
  } catch(e){}
  sessionStorage.removeItem('settings_token');
  showLockScreen();
  showToast('Settings locked');
}

async function checkAuth() {
  const token = sessionStorage.getItem('settings_token');
  if (!token) {
    showLockScreen();
    return;
  }
  try {
    const res = await fetch('/api/settings/auth/status', {
      headers: {'X-Settings-Token': token}
    });
    const data = await res.json();
    if (data.authenticated) {
      unlockScreen();
    } else {
      sessionStorage.removeItem('settings_token');
      showLockScreen();
    }
  } catch(e) {
    showLockScreen();
  }
}

async function loadAll() {
  loadCollectors();
  loadPaperConfig();
  loadStrategyConfig();
}

async function loadCollectors() {
  try {
    const r = await fetch('/api/settings');
    states = await r.json();
    renderCollectors();
  } catch(e) {
    document.getElementById('cards').innerHTML = '<p style="color:#f85149">Failed to load collectors</p>';
  }
}

function renderCollectors() {
  const el = document.getElementById('cards');
  el.innerHTML = SOURCES.map(src => {
    const st = states[src.id] || {};
    const enabled = st.enabled !== false;
    const cardCls = enabled ? 'card active' : 'card paused';
    const badgeTxt = enabled ? 'ACTIVE' : 'PAUSED';
    const badgeCls = enabled ? 'badge badge-green' : 'badge badge-red';
    return `
    <div class="${cardCls}" id="card-${src.id}" style="margin-bottom:12px">
      <div class="card-header" style="margin-bottom:0">
        <div class="card-title">
          <span style="font-size:22px">${src.icon}</span>
          <div>
            <div class="card-name">${src.name} <span class="${badgeCls}">${badgeTxt}</span></div>
            <div style="font-size:12px;color:#8b949e;margin-top:2px">${src.desc}</div>
          </div>
        </div>
        <div class="toggle-wrap">
          <span class="toggle-label" id="lbl-${src.id}">${enabled ? 'ON' : 'OFF'}</span>
          <label class="toggle">
            <input type="checkbox" id="tog-${src.id}" ${enabled ? 'checked' : ''} onchange="toggleCollector('${src.id}')">
            <span class="slider"></span>
          </label>
        </div>
      </div>
    </div>`;
  }).join('');
}

async function toggleCollector(id) {
  const cb = document.getElementById('tog-' + id);
  const enabled = cb.checked;
  try {
    const r = await apiFetch('/api/settings/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({collector: id, enabled}),
    });
    const data = await r.json();
    if (data.ok) {
      states[id] = {...(states[id] || {}), enabled};
      renderCollectors();
      showToast(enabled ? id + ' enabled ✓' : id + ' paused', !enabled);
    } else {
      showToast('Error: ' + (data.error || 'unknown'), true);
      cb.checked = !enabled;
    }
  } catch(e) {
    showToast('Network error', true);
    cb.checked = !enabled;
  }
}

async function loadPaperConfig() {
  try {
    const r = await fetch('/api/paper/config');
    const c = await r.json();
    document.getElementById('p-enabled').checked = c.enabled !== false;
    document.getElementById('p-starting-wallet').value = c.starting_wallet;
    document.getElementById('p-target-wallet').value = c.target_wallet;
    document.getElementById('p-usdt-inr').value = c.usdt_inr;
    document.getElementById('p-leverage').value = c.leverage;
    document.getElementById('p-max-leverage').value = c.max_leverage;
    document.getElementById('p-stop-pct').value = c.stop_pct_of_margin;
    document.getElementById('p-reward-risk').value = c.reward_risk;
    document.getElementById('p-min-confidence').value = c.min_confidence;
    document.getElementById('p-max-concurrent').value = c.max_concurrent;
    document.getElementById('p-max-hold').value = c.max_hold_minutes;
    document.getElementById('p-scaled-sizing').checked = c.scaled_sizing;
    document.getElementById('p-trailing').checked = c.trailing_enabled;
    document.getElementById('p-scaled-leverage').checked = c.scaled_leverage;
    document.getElementById('p-ladder').checked = c.ladder_enabled;
    document.getElementById('p-ladder-tight').checked = c.ladder_tight;
    document.getElementById('p-telegram').checked = c.alert_telegram;
  } catch(e) {
    console.error('Failed to load paper config', e);
  }
}

async function savePaperConfig() {
  const payload = {
    enabled: document.getElementById('p-enabled').checked,
    starting_wallet: parseFloat(document.getElementById('p-starting-wallet').value),
    target_wallet: parseFloat(document.getElementById('p-target-wallet').value),
    usdt_inr: parseFloat(document.getElementById('p-usdt-inr').value),
    leverage: parseFloat(document.getElementById('p-leverage').value),
    max_leverage: parseFloat(document.getElementById('p-max-leverage').value),
    stop_pct_of_margin: parseFloat(document.getElementById('p-stop-pct').value),
    reward_risk: parseFloat(document.getElementById('p-reward-risk').value),
    min_confidence: parseFloat(document.getElementById('p-min-confidence').value),
    max_concurrent: parseInt(document.getElementById('p-max-concurrent').value),
    max_hold_minutes: parseInt(document.getElementById('p-max-hold').value),
    scaled_sizing: document.getElementById('p-scaled-sizing').checked,
    trailing_enabled: document.getElementById('p-trailing').checked,
    scaled_leverage: document.getElementById('p-scaled-leverage').checked,
    ladder_enabled: document.getElementById('p-ladder').checked,
    ladder_tight: document.getElementById('p-ladder-tight').checked,
    alert_telegram: document.getElementById('p-telegram').checked,
  };
  try {
    const res = await apiFetch('/api/paper/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await res.json();
    if (d.ok) showToast('Paper Trading configuration updated ✓');
    else showToast('Save failed: ' + (d.error || 'unknown'), true);
  } catch(e) {
    showToast('Network error saving config', true);
  }
}

async function loadStrategyConfig() {
  try {
    const r = await fetch('/api/strategy/config');
    const c = await r.json();
    document.getElementById('s-groq-model').value = c.groq_model || 'qwen/qwen3.8-27b';
    document.getElementById('s-min-confidence').value = c.crypto_min_confidence;
    document.getElementById('s-bank-size').value = c.bank_size;
    document.getElementById('s-groq-review').checked = c.groq_signal_review_enabled;
    document.getElementById('s-high-conviction').checked = c.high_conviction_only;
    document.getElementById('s-binance-oi').checked = c.binance_oi_enabled;
    document.getElementById('s-orderflow').checked = c.orderflow_enabled;
    document.getElementById('s-binance-klines').checked = c.binance_klines_enabled;
    document.getElementById('s-volume-spike').checked = c.crypto_volume_spike_enabled;
    document.getElementById('s-htf-filter').checked = c.crypto_htf_filter_enabled;
  } catch(e) {
    console.error('Failed to load strategy config', e);
  }
}

async function saveStrategyConfig() {
  const payload = {
    groq_model: document.getElementById('s-groq-model').value.trim(),
    crypto_min_confidence: parseFloat(document.getElementById('s-min-confidence').value),
    bank_size: parseFloat(document.getElementById('s-bank-size').value),
    groq_signal_review_enabled: document.getElementById('s-groq-review').checked,
    high_conviction_only: document.getElementById('s-high-conviction').checked,
    binance_oi_enabled: document.getElementById('s-binance-oi').checked,
    orderflow_enabled: document.getElementById('s-orderflow').checked,
    binance_klines_enabled: document.getElementById('s-binance-klines').checked,
    crypto_volume_spike_enabled: document.getElementById('s-volume-spike').checked,
    crypto_htf_filter_enabled: document.getElementById('s-htf-filter').checked,
  };
  try {
    const res = await apiFetch('/api/strategy/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await res.json();
    if (d.ok) showToast('Strategy & AI configuration updated ✓');
    else showToast('Save failed: ' + (d.error || 'unknown'), true);
  } catch(e) {
    showToast('Network error saving strategy config', true);
  }
}

function showToast(msg, isErr=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  setTimeout(() => t.className = 'toast', 2800);
}

window.addEventListener('DOMContentLoaded', checkAuth);
</script>
</body>
</html>"""


# ── Site-wide theme system ────────────────────────────────────────────────────
# Injected into every page <head>. Theme stored in localStorage ('site_theme'),
# applied as html[data-theme=...]; 'navy' (default) = no attribute, no overrides.
async def _audit_page(request: web.Request) -> web.Response:
    return web.Response(text=_AUDIT_HTML, content_type="text/html")


# The signal post-mortem. Its own route rather than another sidebar tab: this
# is a workbench, it wants the full width for a wide table, and it should be
# reloadable without disturbing whatever the main dashboard was showing.
_AUDIT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Audit — what worked, what did not</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg);color:var(--text);min-height:100vh;padding-bottom:60px;font-size:13px}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:12px 18px;
  display:flex;align-items:center;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:20}
header h1{font-size:16px;font-weight:700;color:var(--text-strong);display:flex;align-items:center;gap:8px}
header .sub{font-size:12px;color:var(--muted)}
a.nav-btn{background:var(--acc-t);border:1px solid var(--acc-t2);color:var(--accent);
  font-size:12px;font-weight:600;padding:5px 12px;border-radius:8px;text-decoration:none;white-space:nowrap}
a.nav-btn:hover{background:var(--acc-t2)}
.spacer{margin-left:auto}
main{padding:16px 18px;max-width:1500px;margin:0 auto}
h2{font-size:14px;font-weight:700;color:var(--text-strong);margin:26px 0 10px;
  display:flex;align-items:baseline;gap:9px}
h2 .hint{font-size:11.5px;font-weight:400;color:var(--muted)}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px}
.card .t{font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted2)}
.card .v{font-size:21px;font-weight:700;color:var(--text-strong);margin-top:3px;line-height:1.15}
.card .s{font-size:11px;color:var(--muted);margin-top:2px}
.pos{color:var(--pos)}.neg{color:var(--neg)}.acc{color:var(--accent)}

.filters{background:var(--panel2);border:1px solid var(--line2);border-radius:10px;
  padding:11px 13px;display:flex;flex-wrap:wrap;gap:9px;align-items:flex-end;margin-top:14px}
.f{display:flex;flex-direction:column;gap:3px}
.f label{font-size:10.5px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted2)}
.f select,.f input{background:var(--sunk);border:1px solid var(--line);color:var(--text);
  border-radius:7px;padding:5px 8px;font-size:12.5px;font-family:inherit;min-width:112px}
.f input[type=search]{min-width:170px}
button.btn{background:var(--acc-t);border:1px solid var(--acc-t2);color:var(--accent);
  border-radius:7px;padding:6px 13px;font-weight:600;font-size:12.5px;cursor:pointer;font-family:inherit}
button.btn:hover{background:var(--acc-t2)}
button.btn.ghost{background:transparent;border-color:var(--line);color:var(--muted)}

.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}
th,td{padding:6px 10px;text-align:left;border-bottom:1px solid var(--line2)}
th{background:var(--panel2);color:var(--muted);font-weight:600;position:sticky;top:0;
  font-size:10.5px;text-transform:uppercase;letter-spacing:.4px;cursor:pointer;user-select:none}
th:hover{color:var(--text-strong)}
th.sorted::after{content:'';margin-left:5px;color:var(--accent)}
th.asc::after{content:'\\2191';margin-left:5px;color:var(--accent)}
th.desc::after{content:'\\2193';margin-left:5px;color:var(--accent)}
tbody tr:hover td{background:var(--panel2)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
th.num{text-align:right}
.empty{color:var(--muted2);padding:16px;font-size:12.5px}

.pill{display:inline-block;padding:1px 8px;border-radius:9999px;font-size:10.5px;font-weight:700;
  letter-spacing:.3px}
.pill.won{background:var(--pos-t);color:var(--pos);border:1px solid var(--pos-t2)}
.pill.lost{background:var(--neg-t);color:var(--neg);border:1px solid var(--neg-t2)}
.pill.expired{background:var(--mut-t);color:var(--muted);border:1px solid var(--line)}
.pill.pending{background:var(--acc-t);color:var(--accent);border:1px solid var(--acc-t2)}
.pill.long{background:var(--pos-t);color:var(--pos)}
.pill.short{background:var(--neg-t);color:var(--neg)}

.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
.tab{background:var(--panel2);border:1px solid var(--line2);color:var(--muted);
  padding:5px 12px;border-radius:9999px;font-size:12px;cursor:pointer;font-weight:600;font-family:inherit}
.tab.on{background:var(--acc-t);border-color:var(--acc-t2);color:var(--accent)}

.bar{position:relative;background:var(--sunk);border-radius:4px;height:16px;min-width:90px;overflow:hidden}
.bar i{position:absolute;left:0;top:0;bottom:0;background:var(--pos-t2);display:block}
.bar span{position:relative;font-size:10.5px;line-height:16px;padding-left:5px;color:var(--text)}

.stage{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-bottom:9px;overflow:hidden}
.stage-hd{padding:10px 13px;display:flex;align-items:center;gap:10px;cursor:pointer;background:var(--panel2)}
.stage-hd b{font-size:13px;color:var(--text-strong)}
.stage-hd .d{font-size:11.5px;color:var(--muted);flex:1;min-width:0}
.stage-hd .n{font-size:11px;color:var(--accent);font-weight:700}
.stage-body{display:none;padding:4px 0}
.stage.open .stage-body{display:block}
.m{padding:8px 13px;border-top:1px solid var(--line2)}
.m code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
  color:var(--accent-soft);word-break:break-word}
.m .kind{font-size:9.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted2);
  border:1px solid var(--line);border-radius:4px;padding:0 4px;margin-right:6px}
.m .sum{font-size:12px;color:var(--muted);margin-top:3px;white-space:normal}
.m .src{font-size:10.5px;color:var(--muted2);margin-top:3px;font-family:ui-monospace,monospace}
.m .mem{font-size:10.5px;color:var(--muted2);margin-top:3px}
.m pre{display:none;white-space:pre-wrap;font-size:11px;color:var(--muted);
  background:var(--sunk);border:1px solid var(--line2);border-radius:7px;padding:9px;margin-top:6px}
.m.open pre{display:block}
.note{font-size:11.5px;color:var(--muted2);margin-top:8px;white-space:normal;line-height:1.55}

@media(max-width:720px){
  html{-webkit-text-size-adjust:100%}
  main{padding:12px}
  header{padding:10px 12px}
  .f select,.f input{min-width:0;width:100%;font-size:16px;min-height:38px}
  .f{flex:1 1 130px}
  .f input[type=search]{min-width:0}
  .cards{grid-template-columns:repeat(auto-fit,minmax(128px,1fr))}
  .card .v{font-size:18px}
  table{font-size:11.5px}
  th,td{padding:6px 7px}
}
</style>
</head>
<body>
<header>
  <h1>&#129514; Signal Audit</h1>
  <span class="sub" id="hdr-sub">loading&hellip;</span>
  <span class="spacer"></span>
  <a class="nav-btn" href="/">&larr; Dashboard</a>
  <a class="nav-btn" href="/data">Database</a>
  <a class="nav-btn" href="/settings">Settings</a>
</header>
<main>

  <div class="cards" id="cards"></div>

  <div class="filters">
    <div class="f"><label>Window</label><select id="f-days" onchange="reload()">
      <option value="1">Last 24h</option><option value="7">Last 7 days</option>
      <option value="30" selected>Last 30 days</option><option value="90">Last 90 days</option>
      <option value="365">Everything</option></select></div>
    <div class="f"><label>Symbol</label><select id="f-symbol" onchange="render()"></select></div>
    <div class="f"><label>Setup</label><select id="f-setup" onchange="render()"></select></div>
    <div class="f"><label>Horizon</label><select id="f-horizon" onchange="render()"></select></div>
    <div class="f"><label>Direction</label><select id="f-direction" onchange="render()"></select></div>
    <div class="f"><label>Result</label><select id="f-outcome" onchange="render()">
      <option value="">All</option><option value="won">Won</option><option value="lost">Lost</option>
      <option value="expired">Expired</option><option value="pending">Pending</option>
      <option value="decided">Decided only</option>
      <option value="netwin">Profitable after costs</option>
      <option value="netloss">Lost money after costs</option></select></div>
    <div class="f"><label>Min &times; cost</label><select id="f-xcost" onchange="render()">
      <option value="0">Any</option><option value="1">1&times;+</option><option value="2">2&times;+</option>
      <option value="3">3&times;+</option><option value="5">5&times;+</option></select></div>
    <div class="f"><label>Min confidence</label><select id="f-conf" onchange="render()">
      <option value="0">Any</option><option value="60">60%+</option><option value="70">70%+</option>
      <option value="80">80%+</option><option value="90">90%+</option></select></div>
    <div class="f"><label>Search</label><input type="search" id="f-q" placeholder="symbol or setup"
      oninput="render()"></div>
    <button class="btn ghost" onclick="clearFilters()">Reset</button>
    <button class="btn" onclick="reload()">Refresh</button>
  </div>

  <h2>Predictions <span class="hint" id="tbl-count"></span></h2>
  <div class="scroll"><table id="tbl">
    <thead><tr>
      <th data-k="timestamp">Fired</th>
      <th data-k="symbol">Symbol</th>
      <th data-k="signal_type">Setup</th>
      <th data-k="direction">Dir</th>
      <th data-k="horizon">Window</th>
      <th class="num" data-k="confidence_pct">Conf</th>
      <th class="num" data-k="entry">Entry</th>
      <th class="num" data-k="target">Target</th>
      <th class="num" data-k="stop">Stop</th>
      <th class="num" data-k="move_pct">Move</th>
      <th class="num" data-k="reward_risk">R:R</th>
      <th class="num" data-k="x_cost">&times; cost</th>
      <th data-k="outcome">Result</th>
      <th class="num" data-k="pnl_pct">Gross</th>
      <th class="num" data-k="net_pnl_pct">Net of fees</th>
    </tr></thead><tbody id="tbody"></tbody>
  </table></div>
  <div class="note">Gross is the price move the signal captured. Net subtracts the round trip
    for that market &mdash; fees, spread and slippage &mdash; which is the number that reaches the
    wallet. A row that is green on gross and red on net is a win that cost money.</div>

  <h2>Where it works and where it does not
    <span class="hint">same signals, sliced by the thing you suspect</span></h2>
  <div class="tabs" id="slice-tabs"></div>
  <div class="scroll"><table>
    <thead><tr>
      <th id="slice-hd">Group</th>
      <th class="num">Fired</th><th class="num">Won</th><th class="num">Lost</th>
      <th class="num">Expired</th><th class="num">Pending</th>
      <th>Hit rate (gross)</th>
      <th class="num">Hit rate net</th>
      <th class="num">Expectancy</th>
      <th class="num">Avg move</th><th class="num">Avg &times; cost</th><th class="num">Avg conf</th>
    </tr></thead><tbody id="slice-body"></tbody>
  </table></div>
  <div class="note">Expectancy is the average net move per signal fired. Positive means the slice
    pays for its own costs; negative means every extra signal there is a slow leak, and no hit
    rate rescues it. Slices with nothing resolved show &mdash; rather than 0% &mdash; an untested
    slice is not a losing one.</div>

  <h2>How a signal is made <span class="hint">every method on the path, read from the code</span></h2>
  <div class="filters" style="margin-top:0">
    <div class="f"><label>Find a method</label>
      <input type="search" id="m-q" placeholder="rsi, atr, vote, tick&hellip;" oninput="renderMethods()"></div>
    <button class="btn ghost" onclick="toggleAllStages(true)">Expand all</button>
    <button class="btn ghost" onclick="toggleAllStages(false)">Collapse all</button>
  </div>
  <div id="methods"></div>
  <div class="note">Signatures, descriptions and line numbers are read out of the modules at
    request time, so this list cannot drift from the code that actually ran.</div>

</main>
<script>
const esc = s => String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const _IST = {timeZone:'Asia/Kolkata'};
let DATA = null, METHODS = null, SLICE = 'by_symbol', SORT = {k:'timestamp', dir:-1};

const SETUP_NAME = {
  rsi_divergence:'Momentum Reversal', volume_spike:'Volume Surge',
  bollinger_squeeze:'Breakout Setup', sentiment_shift:'News Catalyst',
  confluence:'Confluence',
};
const SLICES = [
  ['by_symbol','Symbol'], ['by_setup','Setup'], ['by_horizon','Window'],
  ['by_direction','Direction'], ['by_confidence','Confidence band'],
  ['by_edge','Target vs cost'],
];

function fmtTime(iso){
  if(!iso) return '';
  const d = new Date(/[Z+]|-\\d\\d:\\d\\d$/.test(iso) ? iso : iso + 'Z');
  if(isNaN(d)) return esc(iso);
  return d.toLocaleString('en-IN',{..._IST,day:'2-digit',month:'short',
    hour:'2-digit',minute:'2-digit',hour12:false});
}
function px(v){
  if(v==null) return '&mdash;';
  const a = Math.abs(v);
  return a>=1000 ? v.toLocaleString('en-IN',{maximumFractionDigits:1})
       : a>=1 ? v.toFixed(2) : v.toFixed(4);
}
function pct(v,d){ return v==null ? '&mdash;' : v.toFixed(d==null?2:d)+'%'; }
function signed(v,d){
  if(v==null) return '<span class="sub">&mdash;</span>';
  const cls = v>0?'pos':(v<0?'neg':'');
  return `<span class="${cls}">${v>0?'+':''}${v.toFixed(d==null?3:d)}%</span>`;
}

async function reload(){
  const days = document.getElementById('f-days').value;
  document.getElementById('hdr-sub').textContent = 'loading…';
  try{
    const [a,m] = await Promise.all([
      fetch('/api/audit?days='+days).then(r=>r.json()),
      METHODS ? Promise.resolve({stages:METHODS}) : fetch('/api/audit/methods').then(r=>r.json()),
    ]);
    DATA = a; METHODS = m.stages;
    buildFilterOptions();
    renderCards(); render(); renderMethods();
    document.getElementById('hdr-sub').textContent =
      `${a.records.length} signals · last ${a.days}d · round trip ${a.cost_model.round_trip_pct}%`;
  }catch(e){
    document.getElementById('hdr-sub').textContent = 'failed to load — ' + e;
  }
}

// Options come from the data, not a hardcoded list, so a coin added to the
// watchlist shows up here without a code change.
function fillSelect(id, values, allLabel, pretty){
  const el = document.getElementById(id), keep = el.value;
  el.innerHTML = `<option value="">${allLabel}</option>` +
    values.map(v=>`<option value="${esc(v)}">${esc(pretty?pretty(v):v)}</option>`).join('');
  if(values.includes(keep)) el.value = keep;
}
function buildFilterOptions(){
  const r = DATA.records, uniq = k => [...new Set(r.map(x=>x[k]).filter(Boolean))].sort();
  fillSelect('f-symbol', uniq('symbol'), 'All coins');
  fillSelect('f-setup', uniq('signal_type'), 'All setups', v=>SETUP_NAME[v]||v);
  fillSelect('f-horizon', uniq('horizon'), 'All windows');
  fillSelect('f-direction', uniq('direction'), 'Both ways', v=>v.toUpperCase());
}
function clearFilters(){
  ['f-symbol','f-setup','f-horizon','f-direction','f-outcome'].forEach(i=>document.getElementById(i).value='');
  ['f-xcost','f-conf'].forEach(i=>document.getElementById(i).value='0');
  document.getElementById('f-q').value='';
  render();
}

function filtered(){
  const g = id => document.getElementById(id).value;
  const sym=g('f-symbol'), setup=g('f-setup'), hz=g('f-horizon'), dir=g('f-direction'),
        out=g('f-outcome'), xc=parseFloat(g('f-xcost'))||0, cf=parseFloat(g('f-conf'))||0,
        q=g('f-q').trim().toLowerCase();
  return DATA.records.filter(r=>{
    if(sym && r.symbol!==sym) return false;
    if(setup && r.signal_type!==setup) return false;
    if(hz && r.horizon!==hz) return false;
    if(dir && r.direction!==dir) return false;
    if(out==='decided'){ if(r.outcome!=='won'&&r.outcome!=='lost') return false; }
    else if(out==='netwin'){ if(!r.net_won) return false; }
    else if(out==='netloss'){ if(r.outcome==='pending'||r.net_won) return false; }
    else if(out && r.outcome!==out) return false;
    if(r.x_cost < xc) return false;
    if(r.confidence_pct < cf) return false;
    if(q && !(r.symbol.toLowerCase().includes(q) ||
              (SETUP_NAME[r.signal_type]||r.signal_type).toLowerCase().includes(q))) return false;
    return true;
  });
}

function renderCards(){
  const t = DATA.totals, c = DATA.cost_model;
  document.getElementById('cards').innerHTML = [
    ['Signals fired', t.n, `${t.decided} decided · ${t.pending} still open`,''],
    ['Hit rate, gross', t.hit_rate_pct==null?'&mdash;':t.hit_rate_pct+'%',
      t.decided?`${t.won} won / ${t.lost} lost`:'nothing resolved yet','acc'],
    ['Hit rate, net of fees', t.net_hit_rate_pct==null?'&mdash;':t.net_hit_rate_pct+'%',
      'finished ahead after the round trip',
      t.net_hit_rate_pct!=null && t.net_hit_rate_pct>=50?'pos':'neg'],
    ['Expectancy', t.expectancy_pct==null?'&mdash;':(t.expectancy_pct>0?'+':'')+t.expectancy_pct+'%',
      'avg net move per signal',
      t.expectancy_pct==null?'':(t.expectancy_pct>0?'pos':'neg')],
    ['Avg target', t.avg_move_pct==null?'&mdash;':t.avg_move_pct.toFixed(3)+'%',
      t.avg_x_cost==null?'':t.avg_x_cost.toFixed(1)+'× the round trip',''],
    ['Round trip', c.round_trip_pct+'%',
      `floor ${c.min_target_pct}% at ${c.min_edge_multiple}× cost`,'neg'],
  ].map(([t_,v,s,cls])=>
    `<div class="card"><div class="t">${t_}</div><div class="v ${cls||''}">${v}</div>
     <div class="s">${s||''}</div></div>`).join('');
}

function render(){
  if(!DATA) return;
  const rows = filtered();
  const k = SORT.k;
  rows.sort((a,b)=>{
    const x=a[k], y=b[k];
    if(x===y) return 0;
    if(x==null) return 1;
    if(y==null) return -1;
    return (x>y?1:-1) * SORT.dir;
  });
  document.getElementById('tbl-count').textContent =
    `${rows.length} of ${DATA.records.length} shown`;

  document.getElementById('tbody').innerHTML = rows.length ? rows.map(r=>`<tr>
    <td>${fmtTime(r.timestamp)}</td>
    <td><b>${esc(r.symbol)}</b></td>
    <td>${esc(SETUP_NAME[r.signal_type]||r.signal_type)}</td>
    <td><span class="pill ${esc(r.direction)}">${esc(r.direction.toUpperCase())}</span></td>
    <td>${esc(r.horizon||'—')}</td>
    <td class="num">${r.confidence_pct}%</td>
    <td class="num">${px(r.entry)}</td>
    <td class="num">${px(r.target)}</td>
    <td class="num">${px(r.stop)}</td>
    <td class="num">${pct(r.move_pct,3)}</td>
    <td class="num">${r.reward_risk?r.reward_risk.toFixed(2):'—'}</td>
    <td class="num ${r.x_cost>=3?'pos':(r.x_cost<1?'neg':'')}">${r.x_cost.toFixed(1)}×</td>
    <td><span class="pill ${esc(r.outcome)}">${esc(r.outcome.toUpperCase())}</span></td>
    <td class="num">${r.outcome==='pending'?'<span>—</span>':signed(r.pnl_pct)}</td>
    <td class="num">${r.outcome==='pending'?'<span>—</span>':signed(r.net_pnl_pct)}</td>
  </tr>`).join('') : '<tr><td colspan="15" class="empty">No signals match these filters</td></tr>';

  document.querySelectorAll('#tbl th').forEach(th=>{
    th.classList.toggle('asc', th.dataset.k===k && SORT.dir===1);
    th.classList.toggle('desc', th.dataset.k===k && SORT.dir===-1);
  });
  renderSlices();
}

document.addEventListener('click', e=>{
  const th = e.target.closest('#tbl th');
  if(!th || !th.dataset.k) return;
  if(SORT.k===th.dataset.k) SORT.dir = -SORT.dir; else SORT = {k:th.dataset.k, dir:-1};
  render();
});

function renderSlices(){
  document.getElementById('slice-tabs').innerHTML = SLICES.map(([k,label])=>
    `<button class="tab ${k===SLICE?'on':''}" onclick="SLICE='${k}';renderSlices()">${label}</button>`).join('');
  const label = (SLICES.find(s=>s[0]===SLICE)||[])[1] || 'Group';
  document.getElementById('slice-hd').textContent = label;
  const rows = DATA[SLICE] || [];
  const max = Math.max(1, ...rows.map(r=>r.n));
  document.getElementById('slice-body').innerHTML = rows.length ? rows.map(r=>`<tr>
    <td><b>${esc(SETUP_NAME[r.label]||r.label)}</b></td>
    <td class="num">
      <div class="bar"><i style="width:${r.n/max*100}%"></i><span>${r.n}</span></div></td>
    <td class="num pos">${r.won}</td>
    <td class="num neg">${r.lost}</td>
    <td class="num">${r.expired}</td>
    <td class="num">${r.pending}</td>
    <td>${r.hit_rate_pct==null?'<span class="empty" style="padding:0">not resolved yet</span>'
      :`<div class="bar"><i style="width:${r.hit_rate_pct}%"></i><span>${r.hit_rate_pct}% of ${r.decided}</span></div>`}</td>
    <td class="num">${r.net_hit_rate_pct==null?'&mdash;':r.net_hit_rate_pct+'%'}</td>
    <td class="num">${r.expectancy_pct==null?'&mdash;':signed(r.expectancy_pct)}</td>
    <td class="num">${r.avg_move_pct==null?'&mdash;':r.avg_move_pct.toFixed(3)+'%'}</td>
    <td class="num ${r.avg_x_cost>=3?'pos':(r.avg_x_cost<1?'neg':'')}">${r.avg_x_cost==null?'&mdash;':r.avg_x_cost.toFixed(1)+'×'}</td>
    <td class="num">${r.avg_confidence_pct==null?'&mdash;':r.avg_confidence_pct+'%'}</td>
  </tr>`).join('') : '<tr><td colspan="12" class="empty">Nothing in this window</td></tr>';
}

function renderMethods(){
  if(!METHODS) return;
  const q = (document.getElementById('m-q').value||'').trim().toLowerCase();
  const host = document.getElementById('methods');
  host.innerHTML = METHODS.map((st,i)=>{
    const ms = st.methods.filter(m=> !q ||
      m.name.toLowerCase().includes(q) || m.module.toLowerCase().includes(q) ||
      m.summary.toLowerCase().includes(q));
    if(q && !ms.length) return '';
    return `<div class="stage ${q||i<1?'open':''}">
      <div class="stage-hd" onclick="this.parentNode.classList.toggle('open')">
        <b>${esc(st.stage)}</b>
        <span class="d">${esc(st.description)}</span>
        <span class="n">${ms.length}${ms.length!==st.count?' / '+st.count:''}</span>
      </div>
      <div class="stage-body">${ms.map(m=>`
        <div class="m" onclick="this.classList.toggle('open')">
          <div><span class="kind">${esc(m.kind)}</span><code>${esc(m.signature)}</code></div>
          ${m.summary?`<div class="sum">${esc(m.summary)}</div>`:''}
          ${m.members.length?`<div class="mem">members: ${m.members.map(esc).join(', ')}</div>`:''}
          <div class="src">${esc(m.source)}</div>
          ${m.doc && m.doc!==m.summary?`<pre>${esc(m.doc)}</pre>`:''}
        </div>`).join('')}</div>
    </div>`;
  }).join('') || '<div class="empty">No method matches that search</div>';
}
function toggleAllStages(open){
  document.querySelectorAll('.stage').forEach(s=>s.classList.toggle('open', open));
}

reload();
</script>
</body>
</html>
"""


async def _predict_page(request: web.Request) -> web.Response:
    return web.Response(text=_PREDICT_HTML, content_type="text/html")


# The board. Its own route because it answers a different question from the
# signal feed: signals say "is there a trade worth its costs right now", and
# the honest answer is usually no. This says something about every market all
# the time, and is explicit about how much of it is knowable.
_PREDICT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Price Outlook</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg);color:var(--text);min-height:100vh;padding-bottom:60px;font-size:13px}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:11px 18px;
  display:flex;align-items:center;gap:13px;flex-wrap:wrap;position:sticky;top:0;z-index:30}
header h1{font-size:16px;font-weight:700;color:var(--text-strong);display:flex;gap:8px;align-items:center}
a.nav-btn{background:var(--acc-t);border:1px solid var(--acc-t2);color:var(--accent);font-size:12px;
  font-weight:600;padding:5px 12px;border-radius:8px;text-decoration:none;white-space:nowrap}
.spacer{margin-left:auto}
.tick{font-size:11.5px;color:var(--muted)}
main{padding:16px 18px;max-width:1400px;margin:0 auto}

.surges{display:flex;flex-direction:column;gap:8px;margin-bottom:16px}
.surge{display:flex;align-items:center;gap:11px;background:var(--acc-t);
  border:1px solid var(--acc-t2);border-radius:10px;padding:10px 14px;font-size:13px}
.surge.up{background:var(--pos-t);border-color:var(--pos-t2)}
.surge.down{background:var(--neg-t);border-color:var(--neg-t2)}
.surge b{color:var(--text-strong)}
.surge .tag{font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;font-weight:700;
  padding:3px 8px;border-radius:4px;background:var(--panel);color:var(--accent);white-space:nowrap}
.surge.up .tag{color:var(--pos)} .surge.down .tag{color:var(--neg)}

.note{font-size:12px;color:var(--muted2);line-height:1.6;margin-bottom:14px;max-width:88ch}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}
th,td{padding:11px 14px;text-align:left;border-bottom:1px solid var(--line2)}
th{background:var(--panel2);color:var(--muted2);font-size:10px;letter-spacing:.09em;
  text-transform:uppercase;font-weight:700;position:sticky;top:0}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:var(--panel2)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.sym{font-weight:700;color:var(--text-strong);font-size:14px}
.sub{font-size:11px;color:var(--muted2)}
.pos{color:var(--pos)}.neg{color:var(--neg)}.muted{color:var(--muted)}

/* the forecast cell: a band with the centre marked */
.fc{min-width:160px}
.fc .mid{font-variant-numeric:tabular-nums;font-weight:600;color:var(--text-strong)}
.fc .rng{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.bar{position:relative;height:6px;border-radius:3px;background:var(--sunk);margin-top:5px}
.bar i{position:absolute;top:0;bottom:0;border-radius:3px;opacity:.5}
.bar .now{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--text-strong)}
.bar i.up{background:var(--pos)} .bar i.down{background:var(--neg)} .bar i.flat{background:var(--muted2)}

.pill{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:10.5px;font-weight:700}
.pill.up{background:var(--pos-t);color:var(--pos)}
.pill.down{background:var(--neg-t);color:var(--neg)}
.pill.flat{background:var(--mut-t);color:var(--muted)}
.pill.stale{background:var(--neg-t);color:var(--neg)}
.empty{color:var(--muted2);padding:20px;font-size:13px}

@media(max-width:820px){
  main{padding:12px}
  th,td{padding:9px 10px}
  .h-24h{display:none}
  .fc{min-width:128px}
}
</style>
</head>
<body>
<header>
  <h1>&#128200; Price Outlook</h1>
  <span class="tick" id="tick">loading&hellip;</span>
  <span class="spacer"></span>
  <a class="nav-btn" href="/">&larr; Dashboard</a>
  <a class="nav-btn" href="/audit">Audit</a>
  <a class="nav-btn" href="/api/debug/signals">Diagnostics</a>
</header>
<main>
  <div class="surges" id="surges"></div>
  <div class="note" id="note"></div>
  <div class="scroll"><table>
    <thead><tr>
      <th>Market</th>
      <th class="num">Price</th>
      <th class="num">24h</th>
      <th class="num">Range</th>
      <th class="num">Volume</th>
      <th>Lean</th>
      <th>In 1 hour</th>
      <th>In 4 hours</th>
      <th class="h-24h">In 24 hours</th>
    </tr></thead>
    <tbody id="rows"><tr><td colspan="9" class="empty">loading&hellip;</td></tr></tbody>
  </table></div>
</main>
<script>
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function px(v){
  if(v==null) return '—';
  const a=Math.abs(v);
  return a>=1000?v.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})
       : a>=1 ? v.toFixed(2) : v.toFixed(4);
}
function signed(v,d){
  if(v==null) return '<span class="muted">—</span>';
  const cls=v>0?'pos':(v<0?'neg':'muted');
  return `<span class="${cls}">${v>0?'+':''}${v.toFixed(d==null?2:d)}%</span>`;
}

// One cell: the band, the centre, and where price sits inside it right now.
function cell(f, price){
  if(!f) return '<td class="muted">—</td>';
  const span=f.high-f.low;
  const at=span>0?Math.max(0,Math.min(100,(price-f.low)/span*100)):50;
  return `<td class="fc">
    <div class="mid">${px(f.centre)} ${signed(f.change_pct)}</div>
    <div class="rng">${px(f.low)} – ${px(f.high)}</div>
    <div class="bar"><i class="${f.direction}" style="left:0;right:0"></i>
      <span class="now" style="left:${at}%"></span></div></td>`;
}

async function load(){
  let d;
  try{ d=await fetch('/api/predict').then(r=>r.json()); }
  catch(e){ document.getElementById('tick').textContent='failed — '+e; return; }

  document.getElementById('tick').textContent=
    'updated '+new Date(d.generated_at).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata'})
    +' IST · refreshes every 20s';
  document.getElementById('note').textContent=d.note;

  document.getElementById('surges').innerHTML=(d.surges||[]).map(s=>{
    const dir=s.direction>0?'up':(s.direction<0?'down':'');
    return `<div class="surge ${dir}"><span class="tag">${esc(s.kind)} surge</span>
      <span><b>${esc(s.symbol)}</b> — ${esc(s.detail)}</span></div>`;
  }).join('');

  const rows=d.markets||[];
  document.getElementById('rows').innerHTML = rows.length ? rows.map(m=>{
    const f=Object.fromEntries((m.forecasts||[]).map(x=>[x.label,x]));
    const dir=m.direction||'flat';
    return `<tr>
      <td><span class="sym">${esc(m.symbol.replace('USDT',''))}</span>
        <div class="sub">${esc(m.symbol)}${m.stale
          ? ' <span class="pill stale">stale '+m.data_age_minutes+'m</span>':''}</div></td>
      <td class="num">${px(m.price)}</td>
      <td class="num">${signed(m.change_24h_pct)}</td>
      <td class="num">${m.atr_pct==null?'—':m.atr_pct.toFixed(3)+'%'}
        <div class="sub">per minute</div></td>
      <td class="num">${m.relative_volume==null?'—':'×'+m.relative_volume.toFixed(2)}</td>
      <td><span class="pill ${dir}">${dir.toUpperCase()}</span>
        <div class="sub">${m.confidence==null?'':Math.round(m.confidence*100)+'% agreement'}</div></td>
      ${cell(f['1h'],m.price)}${cell(f['4h'],m.price)}
      <td class="h-24h" style="padding:0">${cell(f['24h'],m.price).replace(/^<td[^>]*>/,'<div class="fc" style="padding:11px 14px">').replace(/<\\/td>$/,'</div>')}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="9" class="empty">No markets with enough history yet.</td></tr>';
}
load(); setInterval(load, 20000);
</script>
</body>
</html>
"""


_THEME_SNIPPET = """
<style>
/* Theme palettes */
/* ── Palette ───────────────────────────────────────────────────────────────
   One variable set drives the sidebar, tables and signal cards. The older
   components are still reskinned by the !important block in the theme
   snippet; these are done properly so a theme actually changes them rather
   than leaving the shell blue while the cards move. */
:root{
  --bg:#15120e; --panel:#201b15; --panel2:#1a1611; --sunk:#0f0c09;
  --line:#3a3128; --line2:#241e18;
  --text:#e8e0d4; --text-strong:#fdf8f0; --muted:#9a8b78; --muted2:#6d6154;
  --accent:#f59e0b; --accent2:#fbbf24; --accent-soft:#fcd34d;
  --pos:#4ade80; --pos-strong:#22c55e; --pos-btn:#16a34a;
  --neg:#f87171; --neg-strong:#ef4444; --neg-btn:#dc2626;

  --pos-t:  color-mix(in srgb, var(--pos-strong) 14%, transparent);
  --pos-t2: color-mix(in srgb, var(--pos-strong) 34%, transparent);
  --neg-t:  color-mix(in srgb, var(--neg-strong) 14%, transparent);
  --neg-t2: color-mix(in srgb, var(--neg-strong) 34%, transparent);
  --acc-t:  color-mix(in srgb, var(--accent) 16%, transparent);
  --acc-t2: color-mix(in srgb, var(--accent) 36%, transparent);
  --mut-t:  color-mix(in srgb, var(--muted) 14%, transparent);
}
html[data-theme="amber"]{
  --bg:#15120e; --panel:#201b15; --panel2:#1a1611; --sunk:#0f0c09;
  --line:#3a3128; --line2:#241e18;
  --text:#e8e0d4; --text-strong:#fdf8f0; --muted:#9a8b78; --muted2:#6d6154;
  --accent:#f59e0b; --accent2:#fbbf24; --accent-soft:#fcd34d;
}
html[data-theme="navy"]{
  --bg:#0f172a; --panel:#1e293b; --panel2:#1b2534; --sunk:#0b1220;
  --line:#334155; --line2:#16202f;
  --text:#e2e8f0; --text-strong:#f1f5f9; --muted:#94a3b8; --muted2:#64748b;
  --accent:#0ea5e9; --accent2:#38bdf8; --accent-soft:#7dd3fc;
}
html[data-theme="carbon"]{
  --bg:#0b0b0c; --panel:#151517; --panel2:#121213; --sunk:#070708;
  --line:#2b2b2f; --line2:#1b1b1e;
  --text:#e4e4e7; --text-strong:#fafafa; --muted:#8b8b93; --muted2:#5f5f66;
  --accent:#a3e635; --accent2:#bef264; --accent-soft:#d9f99d;
}
html[data-theme="crimson"]{
  --bg:#160f11; --panel:#211619; --panel2:#1b1214; --sunk:#0f0a0b;
  --line:#3d2830; --line2:#261a1e;
  --text:#f0dfe3; --text-strong:#fff5f6; --muted:#a8858f; --muted2:#755c64;
  --accent:#fb7185; --accent2:#fda4af; --accent-soft:#fecdd3;
  --pos:#5eead4; --pos-strong:#2dd4bf; --pos-btn:#0d9488;
}
html[data-theme="violet"]{
  --bg:#13111c; --panel:#1c1729; --panel2:#181226; --sunk:#0d0b14;
  --line:#322b4d; --line2:#241f38;
  --text:#e9e4f5; --text-strong:#f5f3fa; --muted:#8b81a8; --muted2:#665d80;
  --accent:#a78bfa; --accent2:#c4b5fd; --accent-soft:#ddd6fe;
}
html[data-theme="emerald"]{
  --bg:#0a1410; --panel:#102219; --panel2:#0c1b13; --sunk:#06100b;
  --line:#1d3b2d; --line2:#12281d;
  --text:#dcefe6; --text-strong:#f0fdf4; --muted:#6b9080; --muted2:#4d6b5d;
  --accent:#34d399; --accent2:#6ee7b7; --accent-soft:#a7f3d0;
}
html[data-theme="light"]{
  --bg:#f4f1ea; --panel:#ffffff; --panel2:#faf8f4; --sunk:#ece7dd;
  --line:#ded7c9; --line2:#eee9df;
  --text:#2c2620; --text-strong:#1a1611; --muted:#7a6f61; --muted2:#9c9285;
  --accent:#b45309; --accent2:#d97706; --accent-soft:#92400e;
  --pos:#15803d; --pos-strong:#16a34a; --pos-btn:#15803d;
  --neg:#b91c1c; --neg-strong:#dc2626; --neg-btn:#b91c1c;
}


/* Overrides applied only when a non-default theme is active */
html[data-theme] body{background:var(--bg)!important;color:var(--text)!important}
html[data-theme] header,html[data-theme] .topbar,html[data-theme] .tab-bar{background:var(--panel)!important;border-color:var(--line)!important}
html[data-theme] header h1,html[data-theme] .topbar h1,html[data-theme] h2,html[data-theme] .tab-btn.active{color:var(--text-strong)!important}
html[data-theme] .card,html[data-theme] .status-card,html[data-theme] .match-card,html[data-theme] .signal-card,html[data-theme] .fb-card,html[data-theme] .fb-sig-card,html[data-theme] .scalp-card,html[data-theme] .mc2,html[data-theme] .wc-group,html[data-theme] .cr-coin,html[data-theme] .cr-sig-card,html[data-theme] .cr-comm-card,html[data-theme] .cr-sig-tab{background:var(--panel)!important;border-color:var(--line)!important}
html[data-theme] .mc2-top,html[data-theme] .mc2-dt,html[data-theme] .mc2-details,html[data-theme] .mc2-ob,html[data-theme] .mc-scoreboard,html[data-theme] .mc-header,html[data-theme] .sc-header,html[data-theme] .sc-footer,html[data-theme] .sc-probs,html[data-theme] .scalp-head,html[data-theme] .scalp-foot,html[data-theme] .fb-header,html[data-theme] .mc-prob,html[data-theme] .mc-odds-box,html[data-theme] .scroll,html[data-theme] .toc a,html[data-theme] .wc-group-hd,html[data-theme] .cr-note,html[data-theme] .cr-input,html[data-theme] .cr-sig-warn,html[data-theme] .cr-glossary,html[data-theme] .cr-page-btn{background:var(--panel2)!important;border-color:var(--line2)!important}
html[data-theme] .card-value,html[data-theme] .mc2-plname,html[data-theme] .mc2-setnow b,html[data-theme] .mvm .val,html[data-theme] .sb-cur,html[data-theme] .sb-sets-total,html[data-theme] .mc-name,html[data-theme] .mc-sets-won,html[data-theme] .mc-game-score,html[data-theme] .sc-bet-player,html[data-theme] .scalp-player,html[data-theme] .fb-team-name,html[data-theme] .fb-score,html[data-theme] .status-val,html[data-theme] .prob-pct,html[data-theme] .mc2-problbl b,html[data-theme] .card-name,html[data-theme] .meta-value,html[data-theme] .sc-conf,html[data-theme] .toc a,html[data-theme] .tbl-head h2,html[data-theme] .cr-coin-sym,html[data-theme] .cr-coin-price,html[data-theme] .cr-comm-price,html[data-theme] .cr-sig-sym{color:var(--text-strong)!important}
html[data-theme] .card-title,html[data-theme] .card-sub,html[data-theme] .refresh,html[data-theme] section h2,html[data-theme] .mc2-lbl span,html[data-theme] .mvm .lab,html[data-theme] .status-name,html[data-theme] .empty,html[data-theme] footer,html[data-theme] .subtitle,html[data-theme] .card-meta,html[data-theme] .meta-label,html[data-theme] .mc2-obimp,html[data-theme] .mc2-obname,html[data-theme] .mc2-setnow,html[data-theme] .mc2-problbl,html[data-theme] .note,html[data-theme] #status,html[data-theme] .mvm .h{color:var(--muted)!important}
html[data-theme] .mc2-sets{color:var(--score)!important}
html[data-theme] .mc2-lbl h3{color:var(--accent)!important}
html[data-theme] .mc2-obval{color:var(--p1)!important}
html[data-theme] .mc2-obval.none{color:var(--muted)!important}
html[data-theme] .mc2-pb1,html[data-theme] .prob-bar-p1{background:var(--p1)!important}
html[data-theme] .mc2-pb2,html[data-theme] .prob-bar-p2{background:var(--p2)!important}
html[data-theme] .mc2-probbar,html[data-theme] .prob-bar-wrap,html[data-theme] .quota-bar{background:var(--barbg)!important}
html[data-theme] .mvm .val.best{background:var(--bestbg)!important;color:var(--best)!important}
html[data-theme] .mc2-ob.fav{border-color:var(--favline)!important;background:var(--favbg)!important}
html[data-theme] .mc2-ob.fav .mc2-obval{color:var(--fav)!important}
html[data-theme] table th{background:var(--panel)!important;color:var(--muted)!important;border-color:var(--line2)!important}
html[data-theme] table td{border-color:var(--line2)!important}
html[data-theme] tr:nth-child(even) td{background:var(--panel2)!important}
html[data-theme] .topbar a{color:var(--accent)!important;border-color:var(--line)!important}
html[data-theme="light"] .src,html[data-theme="light"] .source-tag{background:#dbeafe!important;color:#1d4ed8!important}
/* Theme picker (settings page) */
.theme-row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:26px}
.theme-sw{display:flex;align-items:center;gap:8px;background:transparent;border:2px solid #30363d;color:inherit;font-size:13px;font-weight:700;padding:9px 16px;border-radius:10px;cursor:pointer;font-family:inherit}
.theme-sw.on{border-color:#3fb950;box-shadow:0 0 0 1px #3fb950}
.theme-sw .sw{width:16px;height:16px;border-radius:50%;display:inline-block;border:1px solid rgba(128,128,128,.4)}
</style>
<script>
(function(){
  var t=localStorage.getItem('site_theme')||'amber';
  document.documentElement.setAttribute('data-theme',t);
  // The dots render after this runs, so mark the active one once they exist.
  document.addEventListener('DOMContentLoaded',function(){
    document.querySelectorAll('.side-themes .dot').forEach(function(d){
      d.classList.toggle('on',d.dataset.t===t);});
  });
})();
function setSiteTheme(t){
  localStorage.setItem('site_theme',t);
  document.documentElement.setAttribute('data-theme',t);
  document.querySelectorAll('.theme-sw').forEach(function(b){b.classList.toggle('on',b.dataset.t===t);});
  document.querySelectorAll('.side-themes .dot').forEach(function(d){d.classList.toggle('on',d.dataset.t===t);});
}

// Wrapper around fetch() for the handful of endpoints that now require the
// API bearer token (settings toggle, watchlist edits — see
// scheduler/security.py for why those three and not the read endpoints).
// The token lives in localStorage, entered once via a prompt the first time
// a call comes back 401, and is retried exactly once with the fresh token —
// a second failure means the token itself is wrong and is left to surface
// as an error rather than looping the prompt.
async function apiFetch(url, opts){
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers);
  var token = localStorage.getItem('api_token');
  if(token) opts.headers['Authorization'] = 'Bearer ' + token;
  var res = await fetch(url, opts);
  if(res.status === 401){
    var entered = window.prompt('API token needed for this action (API_AUTH_TOKEN on the server):');
    if(!entered) return res;
    localStorage.setItem('api_token', entered);
    opts.headers['Authorization'] = 'Bearer ' + entered;
    res = await fetch(url, opts);
    if(res.status === 401) localStorage.removeItem('api_token');
  }
  return res;
}

// ── Display currency ────────────────────────────────────────────────────
// Every money figure the server sends is INR — margin, P&L, fees, wallet —
// because position size is computed as qty x price x usdt_inr. Prices are
// already USDT and must never be converted; putting a rupee sign on an
// entry price would simply be wrong.
//
// The rate travels with the data rather than being assumed here: a closed
// trade keeps the rate it was booked at, so reading history back through
// today's rate cannot silently restate it.
//
// The choice is per-browser on purpose. It is a display preference, not
// account state, so it belongs in localStorage rather than a round trip.
window.Money = (function(){
  var KEY = 'display_ccy', ccy = 'USDT';
  try { ccy = localStorage.getItem(KEY) || 'USDT'; } catch(e){}

  function get(){ return ccy; }
  function set(next){
    ccy = (next === 'INR') ? 'INR' : 'USDT';
    try { localStorage.setItem(KEY, ccy); } catch(e){}
    document.dispatchEvent(new CustomEvent('ccychange', {detail: ccy}));
  }
  // rate: what one USDT was worth in INR for THIS figure.
  function fmt(inrValue, rate, signed){
    if(inrValue === null || inrValue === undefined || isNaN(inrValue)) return '—';
    rate = rate || 102;
    var v = (ccy === 'INR') ? inrValue : inrValue / rate;
    var sign = signed ? (v > 0 ? '+' : v < 0 ? '−' : '') : (v < 0 ? '−' : '');
    var body = Math.abs(v).toLocaleString('en-IN',
      {minimumFractionDigits: 2, maximumFractionDigits: 2});
    return sign + (ccy === 'INR' ? '₹' : '') + body + (ccy === 'INR' ? '' : ' USDT');
  }
  function label(){ return ccy === 'INR' ? '₹' : 'USDT'; }

  function mount(){
    var sel = document.getElementById('ccy-select');
    if(!sel) return;
    sel.value = ccy;
    sel.addEventListener('change', function(){ set(sel.value); });
  }
  if(document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', mount);
  else mount();

  return {get: get, set: set, fmt: fmt, label: label};
})();
</script>
"""

# The browser must never re-derive the cost model. These come straight from
# the same ScalpConfig the analyzers use, so recalibrating fees updates the
# page and the engine together.
_HTML = (
    _HTML
    # The FULL round trip — brokerage, the spread crossed twice, and what
    # fills actually cost beyond it. This used to be brokerage alone, so every
    # "x cost" chip on the site read 1.42x higher than the trade really earns:
    # a 0.845% ETH target showed as 7.2x when the engine, and /audit, scored
    # the same target at 5.0x. Two pages disagreeing about the same number is
    # how a losing edge gets believed.
    .replace("__BREAK_EVEN_PCT__", f"{_SCALP.cost_floor_pct * 100:.4f}")
    .replace("__MIN_TARGET_PCT__", f"{_SCALP.min_target_pct * 100:.4f}")
    .replace("__PAPER_LEVERAGE__", f"{_SETTINGS.paper_leverage:g}")
)
_HTML = _HTML.replace("</head>", _THEME_SNIPPET + "</head>")
_DATA_HTML = _DATA_HTML.replace("</head>", _THEME_SNIPPET + "</head>")
_SETTINGS_HTML = _SETTINGS_HTML.replace("</head>", _THEME_SNIPPET + "</head>")
_AUDIT_HTML = _AUDIT_HTML.replace("</head>", _THEME_SNIPPET + "</head>")
_PREDICT_HTML = _PREDICT_HTML.replace("</head>", _THEME_SNIPPET + "</head>")


async def make_app(runner) -> web.Application:
    from scheduler.security import rate_limit_middleware, security_headers_middleware

    # Order matters: rate limiting runs first so a limited request never
    # reaches a handler at all, and both wrap every handler uniformly rather
    # than being opted into per-route — a cross-cutting concern bolted onto
    # individual handlers is the one that gets forgotten on the next new
    # endpoint.
    app = web.Application(middlewares=[
        rate_limit_middleware(lambda: _SETTINGS),
        security_headers_middleware,
    ])

    def _bind(handler_fn):
        async def _bound(req: web.Request) -> web.Response:
            return await handler_fn(runner, req)
        return _bound

    app.router.add_get("/", _dashboard)
    app.router.add_get("/data", _data_page)
    app.router.add_get("/api/tables", _bind(_api_tables))
    app.router.add_get("/health", _bind(_health))
    app.router.add_get("/api/status", _bind(_api_status))
    app.router.add_get("/api/debug", _bind(_api_debug))
    app.router.add_get("/settings", _settings_page)
    app.router.add_get("/api/settings", _bind(_api_collector_states))
    app.router.add_post("/api/auth/verify", _bind(_api_auth_verify))
    app.router.add_post("/api/settings/auth/login", _bind(_api_settings_auth_login))
    app.router.add_get("/api/settings/auth/status", _bind(_api_settings_auth_status))
    app.router.add_post("/api/settings/auth/logout", _bind(_api_settings_auth_logout))
    app.router.add_get("/api/paper/config", _bind(_api_paper_config_get))
    app.router.add_post("/api/paper/config", _bind(_api_paper_config_post))
    app.router.add_get("/api/strategy/config", _bind(_api_strategy_config_get))
    app.router.add_post("/api/strategy/config", _bind(_api_strategy_config_post))
    app.router.add_post("/api/settings/toggle", _bind(_api_collector_toggle))
    # Crypto & Commodities Routes
    app.router.add_get("/api/crypto/coins", _bind(_api_crypto_coins))
    app.router.add_get("/api/crypto/signals", _bind(_api_crypto_signals))
    app.router.add_get("/api/crypto/forecasts", _bind(_api_crypto_forecasts))
    app.router.add_get("/api/paper", _bind(_api_paper))
    app.router.add_get("/api/debug/coindcx", _bind(_api_debug_coindcx))
    app.router.add_post("/api/sentiment/ingest", _bind(_api_sentiment_ingest))
    app.router.add_get("/api/sentiment/recent", _bind(_api_sentiment_recent))
    app.router.add_get("/api/signals/history", _bind(_api_signal_history))
    app.router.add_get("/api/debug/signals", _bind(_api_debug_signals))
    app.router.add_get("/predict", _predict_page)
    app.router.add_get("/api/predict", _bind(_api_predict))
    app.router.add_get("/api/signals/accuracy", _bind(_api_signal_accuracy))
    app.router.add_get("/audit", _audit_page)
    app.router.add_get("/api/audit", _bind(_api_audit))
    app.router.add_get("/api/audit/methods", _bind(_api_audit_methods))
    app.router.add_get("/api/audit/reviewer", _bind(_api_reviewer_scorecard))
    app.router.add_get("/api/debug/volume", _bind(_api_debug_volume))
    app.router.add_post("/api/crypto/watchlist/add", _bind(_api_crypto_watchlist_add))
    app.router.add_post("/api/crypto/watchlist/remove", _bind(_api_crypto_watchlist_remove))
    app.router.add_get("/api/commodities", _bind(_api_commodities))
    app.router.add_get("/api/debug/binance", _bind(_api_binance_probe))
    return app


async def _dashboard(request: web.Request) -> web.Response:
    return web.Response(text=_HTML, content_type="text/html")


async def start_health_server(runner, port: int = 8080) -> web.AppRunner:
    app = await make_app(runner)
    web_runner = web.AppRunner(app)
    await web_runner.setup()
    site = web.TCPSite(web_runner, "0.0.0.0", port)
    await site.start()
    return web_runner
