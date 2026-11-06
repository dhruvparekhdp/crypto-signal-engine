"""Read-only trading-v1 API for this engine.

    python -m scheduler.integration_v1

Binds 127.0.0.1:8091 unless INTEGRATION_BIND / INTEGRATION_PORT say otherwise.
Every request needs Authorization: Bearer <INTEGRATION_API_TOKEN>. The token
is read from the process environment. An empty token refuses every request.

This process only reads the paper book. It does not import the order modules
and it does not place, cancel, or modify anything.

Envelope (every response):
    source_engine, schema_version, generated_at, data_as_of, status,
    is_stale, mode, correlation_id, data, errors.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from aiohttp import web
from sqlalchemy import and_, case, func, or_, select

from config.settings import settings
from storage.database import AsyncSessionFactory
from storage.models import (
    CryptoSignalLog,
    CryptoSnapshot,
    PaperCycle,
    PaperPosition,
    PaperTrade,
    PaperTradingConfig,
    StrategyRegistry,
)

log = logging.getLogger("integration_v1")

SOURCE_ENGINE = "crypto-signal-engine"
SCHEMA_VERSION = "1.0"
MODE = "paper"
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8091
DEFAULT_STALE_SECONDS = 900
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
QUOTE_CURRENCY = "USDT"
ACCOUNT_CURRENCY = "INR"

PATHS = (
    "/integration/v1/health",
    "/integration/v1/market/overview",
    "/integration/v1/signals",
    "/integration/v1/positions",
    "/integration/v1/trades",
    "/integration/v1/performance",
    "/integration/v1/risk",
    "/integration/v1/strategies",
)


def bind_address() -> tuple[str, int]:
    """Loopback unless INTEGRATION_BIND is set. Port from INTEGRATION_PORT."""
    host = os.environ.get("INTEGRATION_BIND", DEFAULT_BIND).strip() or DEFAULT_BIND
    raw = os.environ.get("INTEGRATION_PORT", str(DEFAULT_PORT)).strip()
    return host, int(raw or DEFAULT_PORT)


def _env_token() -> str:
    return os.environ.get("INTEGRATION_API_TOKEN", "").strip()


def _env_stale_seconds() -> int:
    raw = os.environ.get("INTEGRATION_STALE_SECONDS", str(DEFAULT_STALE_SECONDS))
    value = int(raw.strip() or DEFAULT_STALE_SECONDS)
    if value < 1:
        raise ValueError("INTEGRATION_STALE_SECONDS must be at least 1")
    return value


def _iso(dt: datetime | None) -> str:
    if dt is None:
        dt = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    text = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        text = f"{text}.{dt.microsecond:06d}"
    return text + "Z"


def _naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def _dec(value: Any, places: int = 8) -> str:
    try:
        number = Decimal(str(0 if value is None else value))
    except Exception:
        number = Decimal(0)
    quant = Decimal(1).scaleb(-places)
    return format(number.quantize(quant, rounding=ROUND_HALF_UP), "f")


def _money(value: Any, currency: str, places: int = 2) -> dict[str, str]:
    return {"amount": _dec(value, places), "currency": currency}


def _token_matches(supplied: str, expected: str) -> bool:
    if not supplied or not expected:
        return False
    left = hashlib.sha256(supplied.encode()).digest()
    right = hashlib.sha256(expected.encode()).digest()
    return hmac.compare_digest(left, right)


def _parse_since(raw: str) -> tuple[datetime, int | None]:
    text = raw.strip()
    row_id: int | None = None
    if "|" in text:
        text, id_text = text.rsplit("|", 1)
        row_id = int(id_text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return _naive_utc(parsed), row_id


class Service:
    def __init__(self, sessions, token: str, stale_after: timedelta):
        self.sessions = sessions
        self.token = token
        self.stale_after = stale_after

    def envelope(
        self,
        request: web.Request,
        *,
        data: dict,
        status: str,
        errors: list[dict[str, str]],
        data_as_of: datetime | None,
        is_stale: bool,
        http_status: int = 200,
    ) -> web.Response:
        now = datetime.now(UTC)
        body = {
            "source_engine": SOURCE_ENGINE,
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso(now),
            "data_as_of": _iso(data_as_of or now),
            "status": status,
            "is_stale": is_stale,
            "mode": MODE,
            "correlation_id": request.headers.get("X-Correlation-ID", ""),
            "data": data,
            "errors": errors,
        }
        return web.json_response(body, status=http_status)

    def fail(
        self,
        request: web.Request,
        http_status: int,
        code: str,
        message: str,
        *,
        data_status: str = "unavailable",
    ) -> web.Response:
        return self.envelope(
            request,
            data={},
            status=data_status,
            errors=[{"code": code, "message": message}],
            data_as_of=None,
            is_stale=False,
            http_status=http_status,
        )

    def authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return _token_matches(header[7:].strip(), self.token)

    def freshness(self, latest: datetime | None) -> tuple[bool, datetime, str, list]:
        """is_stale, data_as_of, status, errors. No timestamp means stale."""
        now = datetime.now(UTC).replace(tzinfo=None)
        if latest is None:
            return True, now, "degraded", [
                {"code": "no_data", "message": "no rows for this view yet"},
            ]
        stale = (now - latest) > self.stale_after
        if stale:
            return True, latest, "degraded", [
                {"code": "stale", "message": "last update is older than the threshold"},
            ]
        return False, latest, "ok", []

    def list_query(self, request: web.Request):
        raw_limit = request.query.get("limit")
        if raw_limit is None or raw_limit == "":
            limit = DEFAULT_LIMIT
        else:
            try:
                limit = int(raw_limit)
            except ValueError:
                return None, self.fail(
                    request, 400, "bad_limit", "limit must be an integer",
                )
            if limit < 1:
                return None, self.fail(
                    request, 400, "bad_limit", "limit must be at least 1",
                )
            if limit > MAX_LIMIT:
                return None, self.fail(
                    request, 400, "limit_too_large", "limit cannot exceed 500",
                )
        raw_since = request.query.get("since", "")
        if not raw_since:
            return (limit, None, None), None
        try:
            since, row_id = _parse_since(raw_since)
        except ValueError:
            return None, self.fail(
                request, 400, "bad_since", "since must be ISO-8601",
            )
        return (limit, since, row_id), None


SVC_KEY: web.AppKey[Service] = web.AppKey("svc")


def make_app(sessions=None, *, token: str | None = None, stale_after_s: int | None = None):
    seconds = DEFAULT_STALE_SECONDS if stale_after_s is None else stale_after_s
    if seconds < 1:
        raise ValueError("stale_after_s must be at least 1")
    app = web.Application()
    app[SVC_KEY] = Service(
        sessions or AsyncSessionFactory,
        _env_token() if token is None else token,
        timedelta(seconds=seconds),
    )
    for path in PATHS:
        app.router.add_route("*", path, _dispatch)
    app.router.add_route("*", "/integration/v1", _dispatch)
    app.router.add_route("*", "/integration/v1/{rest:.*}", _dispatch)
    return app


async def _dispatch(request: web.Request) -> web.Response:
    svc: Service = request.app[SVC_KEY]
    if request.method != "GET":
        return svc.fail(
            request, 405, "method_not_allowed", "only GET is served",
        )
    handler = _GET.get(request.path)
    if handler is None:
        return svc.fail(request, 404, "not_found", "unknown path")
    if not svc.authorized(request):
        return svc.fail(request, 401, "unauthorized", "bearer token required")
    try:
        return await handler(svc, request)
    except Exception:
        log.exception("integration_v1_read_failed")
        return svc.fail(
            request, 503, "unavailable", "data source unavailable",
        )


def _after(time_col, id_col, since: datetime | None, row_id: int | None):
    if since is None:
        return None
    if row_id is None:
        return time_col > since
    return or_(time_col > since, and_(time_col == since, id_col > row_id))


async def _page(session, model, time_col, since, row_id, limit):
    stmt = select(model)
    cond = _after(time_col, model.id, since, row_id)
    if cond is not None:
        stmt = stmt.where(cond)
    stmt = stmt.order_by(time_col.asc(), model.id.asc()).limit(limit + 1)
    rows = list((await session.scalars(stmt)).all())
    more = len(rows) > limit
    return rows[:limit], more


def _cursor(when: datetime, row_id: int, more: bool) -> str | None:
    if not more:
        return None
    return f"{_iso(when)}|{row_id}"


async def _max_time(session, column):
    return await session.scalar(select(func.max(column)))


async def _health(svc: Service, request: web.Request) -> web.Response:
    async with svc.sessions() as session:
        stamps = {
            "snapshots": await _max_time(session, CryptoSnapshot.timestamp),
            "signals": await _max_time(session, CryptoSignalLog.timestamp),
            "positions": await _max_time(session, PaperPosition.opened_at),
            "trades": await _max_time(session, PaperTrade.closed_at),
        }
    present = [value for value in stamps.values() if value is not None]
    latest = max(present) if present else None
    stale, as_of, status, errors = svc.freshness(latest)
    data = {
        "database": "ok",
        "stale_after_seconds": int(svc.stale_after.total_seconds()),
        "sources": {key: _iso(value) if value else None for key, value in stamps.items()},
    }
    return svc.envelope(
        request, data=data, status=status, errors=errors,
        data_as_of=as_of, is_stale=stale,
    )


async def _overview(svc: Service, request: web.Request) -> web.Response:
    async with svc.sessions() as session:
        latest = await _max_time(session, CryptoSnapshot.timestamp)
        newest_id = (
            select(
                CryptoSnapshot.symbol.label("symbol"),
                func.max(CryptoSnapshot.id).label("max_id"),
            )
            .group_by(CryptoSnapshot.symbol)
            .subquery()
        )
        rows = list((await session.scalars(
            select(CryptoSnapshot).join(
                newest_id, CryptoSnapshot.id == newest_id.c.max_id,
            )
        )).all())
    quotes = []
    for row in rows:
        quotes.append({
            "symbol": row.symbol,
            "price": _dec(row.price),
            "currency": QUOTE_CURRENCY,
            "volume_24h": _dec(row.volume_24h),
            "rsi_14": _dec(row.rsi_14),
            "as_of": _iso(row.timestamp),
        })
    quotes.sort(key=lambda item: item["symbol"])
    stale, as_of, status, errors = svc.freshness(latest)
    return svc.envelope(
        request,
        data={"quotes": quotes, "quote_count": len(quotes)},
        status=status, errors=errors, data_as_of=as_of, is_stale=stale,
    )


def _signal_item(row: CryptoSignalLog) -> dict:
    return {
        "id": row.id,
        "symbol": row.symbol,
        "signal_type": row.signal_type,
        "direction": row.direction,
        "timeframe": row.timeframe,
        "trade_mode": row.trade_mode,
        "outcome": row.outcome,
        "suppressed_by": row.suppressed_by,
        "skip_reason": row.skip_reason,
        "price": _dec(row.current_price),
        "target_price": _dec(row.target_price),
        "stop_price": _dec(row.stop_loss),
        "price_currency": QUOTE_CURRENCY,
        "confidence": _dec(row.confidence),
        "at": _iso(row.timestamp),
    }


async def _signals(svc: Service, request: web.Request) -> web.Response:
    parsed, err = svc.list_query(request)
    if err is not None:
        return err
    limit, since, row_id = parsed
    async with svc.sessions() as session:
        latest = await _max_time(session, CryptoSignalLog.timestamp)
        rows, more = await _page(
            session, CryptoSignalLog, CryptoSignalLog.timestamp, since, row_id, limit,
        )
    stale, as_of, status, errors = svc.freshness(latest)
    cursor = _cursor(rows[-1].timestamp, rows[-1].id, more) if rows else None
    return svc.envelope(
        request,
        data={"items": [_signal_item(row) for row in rows], "next_cursor": cursor},
        status=status, errors=errors, data_as_of=as_of, is_stale=stale,
    )


def _position_item(row: PaperPosition) -> dict:
    return {
        "id": row.id,
        "symbol": row.symbol,
        "side": row.side,
        "trade_mode": row.trade_mode,
        "signal_type": row.signal_type,
        "timeframe": row.timeframe,
        "quantity": _dec(row.coin_qty),
        "entry_price": _dec(row.entry_price),
        "stop_price": _dec(row.stop_price),
        "target_price": _dec(row.target_price),
        "price_currency": QUOTE_CURRENCY,
        "leverage": _dec(row.leverage),
        "margin": _money(row.margin, ACCOUNT_CURRENCY),
        "opened_at": _iso(row.opened_at),
    }


async def _positions(svc: Service, request: web.Request) -> web.Response:
    parsed, err = svc.list_query(request)
    if err is not None:
        return err
    limit, since, row_id = parsed
    async with svc.sessions() as session:
        latest = await _max_time(session, PaperPosition.opened_at)
        rows, more = await _page(
            session, PaperPosition, PaperPosition.opened_at, since, row_id, limit,
        )
    stale, as_of, status, errors = svc.freshness(latest)
    cursor = _cursor(rows[-1].opened_at, rows[-1].id, more) if rows else None
    return svc.envelope(
        request,
        data={"items": [_position_item(row) for row in rows], "next_cursor": cursor},
        status=status, errors=errors, data_as_of=as_of, is_stale=stale,
    )


def _trade_item(row: PaperTrade) -> dict:
    return {
        "id": row.id,
        "symbol": row.symbol,
        "side": row.side,
        "trade_mode": row.trade_mode,
        "signal_type": row.signal_type,
        "timeframe": row.timeframe,
        "exit_reason": row.exit_reason,
        "quantity": _dec(row.coin_qty),
        "entry_price": _dec(row.entry_price),
        "exit_price": _dec(row.exit_price),
        "price_currency": QUOTE_CURRENCY,
        "leverage": _dec(row.leverage),
        "margin": _money(row.margin, ACCOUNT_CURRENCY),
        "gross_pnl": _money(row.gross_pnl, ACCOUNT_CURRENCY),
        "fees": _money(row.trading_fees, ACCOUNT_CURRENCY),
        "funding": _money(row.funding_paid, ACCOUNT_CURRENCY),
        "net_pnl": _money(row.net_pnl, ACCOUNT_CURRENCY),
        "opened_at": _iso(row.opened_at),
        "closed_at": _iso(row.closed_at),
    }


async def _trades(svc: Service, request: web.Request) -> web.Response:
    parsed, err = svc.list_query(request)
    if err is not None:
        return err
    limit, since, row_id = parsed
    async with svc.sessions() as session:
        latest = await _max_time(session, PaperTrade.closed_at)
        rows, more = await _page(
            session, PaperTrade, PaperTrade.closed_at, since, row_id, limit,
        )
    stale, as_of, status, errors = svc.freshness(latest)
    cursor = _cursor(rows[-1].closed_at, rows[-1].id, more) if rows else None
    return svc.envelope(
        request,
        data={"items": [_trade_item(row) for row in rows], "next_cursor": cursor},
        status=status, errors=errors, data_as_of=as_of, is_stale=stale,
    )


async def _performance(svc: Service, request: web.Request) -> web.Response:
    raw_since = request.query.get("since", "")
    since = None
    if raw_since:
        try:
            since, _row_id = _parse_since(raw_since)
        except ValueError:
            return svc.fail(request, 400, "bad_since", "since must be ISO-8601")
    async with svc.sessions() as session:
        stmt = select(
            func.count(PaperTrade.id),
            func.coalesce(func.sum(PaperTrade.net_pnl), 0),
            func.coalesce(func.sum(PaperTrade.gross_pnl), 0),
            func.coalesce(func.sum(PaperTrade.trading_fees), 0),
            func.coalesce(func.sum(PaperTrade.funding_paid), 0),
            func.coalesce(
                func.sum(case((PaperTrade.net_pnl > 0, 1), else_=0)), 0,
            ),
            func.coalesce(
                func.sum(case((PaperTrade.net_pnl < 0, 1), else_=0)), 0,
            ),
            func.max(PaperTrade.closed_at),
        )
        if since is not None:
            stmt = stmt.where(PaperTrade.closed_at > since)
        row = (await session.execute(stmt)).one()
        count, net, gross, fees, funding, wins, losses, latest = row
    count = int(count or 0)
    wins = int(wins or 0)
    losses = int(losses or 0)
    rate = (Decimal(wins) / Decimal(count)) if count else Decimal(0)
    stale, as_of, status, errors = svc.freshness(latest)
    data = {
        "closed_trades": count,
        "wins": wins,
        "losses": losses,
        "win_rate": _dec(rate),
        "net_pnl": _money(net, ACCOUNT_CURRENCY),
        "gross_pnl": _money(gross, ACCOUNT_CURRENCY),
        "fees": _money(fees, ACCOUNT_CURRENCY),
        "funding": _money(funding, ACCOUNT_CURRENCY),
    }
    return svc.envelope(
        request, data=data, status=status, errors=errors,
        data_as_of=as_of, is_stale=stale,
    )


async def _risk(svc: Service, request: web.Request) -> web.Response:
    async with svc.sessions() as session:
        positions = list((await session.scalars(select(PaperPosition))).all())
        cycle = (await session.scalars(
            select(PaperCycle).order_by(PaperCycle.id.desc()).limit(1)
        )).first()
        config = await session.get(PaperTradingConfig, 1)
        latest_pos = await _max_time(session, PaperPosition.opened_at)
    margin = sum((Decimal(str(row.margin or 0)) for row in positions), Decimal(0))
    notional = sum(
        (
            Decimal(str(row.entry_price or 0)) * Decimal(str(row.coin_qty or 0))
            for row in positions
        ),
        Decimal(0),
    )
    latest = latest_pos
    if cycle is not None and (latest is None or cycle.started_at > latest):
        latest = cycle.started_at
    wallet = cycle.wallet if cycle is not None else 0
    peak = cycle.peak_wallet if cycle is not None else 0
    drawdown = max(Decimal(str(peak)) - Decimal(str(wallet)), Decimal(0))
    stale, as_of, status, errors = svc.freshness(latest)
    data = {
        "open_positions": len(positions),
        "margin_in_use": _money(margin, ACCOUNT_CURRENCY),
        "open_notional": _money(notional, QUOTE_CURRENCY, 8),
        "max_concurrent": config.max_concurrent if config is not None else None,
        "wallet": _money(wallet, ACCOUNT_CURRENCY),
        "peak_wallet": _money(peak, ACCOUNT_CURRENCY),
        "drawdown": _money(drawdown, ACCOUNT_CURRENCY),
        "cycle_status": cycle.status if cycle is not None else None,
    }
    return svc.envelope(
        request, data=data, status=status, errors=errors,
        data_as_of=as_of, is_stale=stale,
    )


def _settings_strategies() -> list[dict]:
    raw = settings.swing_strategies or ""
    items = []
    now = datetime.now(UTC).replace(tzinfo=None)
    for index, part in enumerate((piece.strip() for piece in raw.split(",")), start=1):
        if not part:
            continue
        spec, _, params = part.partition(":")
        timeframe, _, name = spec.partition("@")
        items.append({
            "id": index,
            "spec_key": spec,
            "timeframe": timeframe,
            "name": name or spec,
            "params": params,
            "status": "configured",
            "source": "settings",
            "updated_at": _iso(now),
            "_sort_at": now,
        })
    return items


def _registry_item(row: StrategyRegistry) -> dict:
    spec = row.spec_key
    timeframe, _, name = spec.partition("@")
    return {
        "id": row.id,
        "spec_key": spec,
        "timeframe": timeframe if name else "",
        "name": name or spec,
        "params": row.params,
        "status": row.status,
        "source": "registry",
        "updated_at": _iso(row.updated_at),
        "_sort_at": row.updated_at,
    }


async def _strategies(svc: Service, request: web.Request) -> web.Response:
    parsed, err = svc.list_query(request)
    if err is not None:
        return err
    limit, since, row_id = parsed
    async with svc.sessions() as session:
        rows = list((await session.scalars(
            select(StrategyRegistry).order_by(
                StrategyRegistry.updated_at.asc(), StrategyRegistry.id.asc(),
            )
        )).all())
        latest = await _max_time(session, StrategyRegistry.updated_at)
    if rows:
        items = [_registry_item(row) for row in rows]
        stale, as_of, status, errors = svc.freshness(latest)
    else:
        items = _settings_strategies()
        latest = items[0]["_sort_at"] if items else None
        stale, as_of, status, errors = svc.freshness(latest)
    chosen = []
    for item in items:
        stamp = item["_sort_at"]
        if since is not None and stamp < since:
            continue
        if since is not None and stamp == since and (row_id is None or item["id"] <= row_id):
            continue
        public = {key: value for key, value in item.items() if key != "_sort_at"}
        chosen.append((public, stamp))
    page = chosen[: limit + 1]
    more = len(page) > limit
    page = page[:limit]
    cursor = _cursor(page[-1][1], page[-1][0]["id"], more) if page else None
    return svc.envelope(
        request,
        data={"items": [item for item, _stamp in page], "next_cursor": cursor},
        status=status, errors=errors, data_as_of=as_of, is_stale=stale,
    )


_GET = {
    "/integration/v1/health": _health,
    "/integration/v1/market/overview": _overview,
    "/integration/v1/signals": _signals,
    "/integration/v1/positions": _positions,
    "/integration/v1/trades": _trades,
    "/integration/v1/performance": _performance,
    "/integration/v1/risk": _risk,
    "/integration/v1/strategies": _strategies,
}


def main() -> None:
    host, port = bind_address()
    if not _env_token():
        log.warning("integration_api_token_unset")
    web.run_app(make_app(stale_after_s=_env_stale_seconds()), host=host, port=port)


if __name__ == "__main__":
    main()
