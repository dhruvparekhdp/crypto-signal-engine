"""
Binance USD-M futures REST client for the live swing book: just the calls it needs, signed with the account's key.

The key needs "Enable Futures" only — never withdrawals — and should be restricted to the server's IP. It is read
from settings (environment), never logged, never returned by any endpoint.

Conditional orders (stop-market, take-profit-market) are placed with closePosition=true at the MARK price, so they
close whatever is open even if the bot is down. Binance moved conditional orders to its algo-order endpoint; if the
classic endpoint refuses them, the algo endpoint is used instead.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import time
from dataclasses import dataclass
from urllib.parse import urlencode

LIVE_URL = "https://fapi.binance.com"
TESTNET_URL = "https://testnet.binancefuture.com"


class BinanceError(RuntimeError):
    def __init__(self, status: int, code: int | None, msg: str):
        super().__init__(f"binance {status} {code}: {msg}")
        self.status, self.code, self.msg = status, code, msg


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    step: float          # quantity step (MARKET_LOT_SIZE, else LOT_SIZE)
    min_qty: float
    tick: float          # price step
    min_notional: float  # smallest order value in USDT

    def floor_qty(self, q: float) -> float:
        n = math.floor(q / self.step + 1e-9)
        return round(n * self.step, _decimals(self.step))

    def round_price(self, p: float) -> float:
        return round(round(p / self.tick) * self.tick, _decimals(self.tick))


def _decimals(step: float) -> int:
    s = f"{step:.12f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def parse_rules(info: dict) -> dict[str, SymbolRules]:
    out = {}
    for s in info.get("symbols", []):
        if s.get("contractType") not in (None, "PERPETUAL") or s.get("status", "TRADING") != "TRADING":
            continue
        f = {x["filterType"]: x for x in s.get("filters", [])}
        lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {}
        price = f.get("PRICE_FILTER", {})
        notional = f.get("MIN_NOTIONAL", {})
        out[s["symbol"]] = SymbolRules(
            s["symbol"], float(lot.get("stepSize", 0.001)), float(lot.get("minQty", 0.0)),
            float(price.get("tickSize", 0.0001)), float(notional.get("notional", notional.get("minNotional", 5.0))))
    return out


class BinanceFutures:
    def __init__(self, api_key: str, api_secret: str, base_url: str = LIVE_URL, client=None, recv_window: int = 5000):
        self._key, self._secret = api_key, api_secret.encode()
        self.base = base_url.rstrip("/")
        self._client = client
        self.recv_window = recv_window
        self._rules: dict[str, SymbolRules] = {}
        self._rules_at = 0.0
        self._time_offset_ms = 0

    # ── plumbing ───────────────────────────────────────────────
    def _sign(self, params: dict) -> str:
        q = urlencode(params)
        return q + "&signature=" + hmac.new(self._secret, q.encode(), hashlib.sha256).hexdigest()

    async def _request(self, method: str, path: str, params: dict | None = None, signed: bool = True):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {"X-MBX-APIKEY": self._key}
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
            params["recvWindow"] = self.recv_window
            query = self._sign(params)
        else:
            query = urlencode(params)
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        r = await self._client.request(method, url, headers=headers, timeout=15)
        try:
            body = r.json()
        except ValueError:
            body = {"msg": r.text[:200]}
        if r.status_code >= 400 or (isinstance(body, dict) and isinstance(body.get("code"), int) and body["code"] < 0):
            code = body.get("code") if isinstance(body, dict) else None
            raise BinanceError(r.status_code, code, (body.get("msg") if isinstance(body, dict) else str(body))[:200])
        return body

    async def sync_time(self) -> None:
        """Clock drift beyond recvWindow makes every signed call fail (-1021); measure it once."""
        body = await self._request("GET", "/fapi/v1/time", signed=False)
        self._time_offset_ms = int(body["serverTime"]) - int(time.time() * 1000)

    # ── reads ──────────────────────────────────────────────────
    async def rules(self, symbol: str) -> SymbolRules:
        if not self._rules or time.time() - self._rules_at > 6 * 3600:
            self._rules = parse_rules(await self._request("GET", "/fapi/v1/exchangeInfo", signed=False))
            self._rules_at = time.time()
        return self._rules[symbol.upper()]

    async def usdt_balance(self) -> dict:
        for a in await self._request("GET", "/fapi/v2/balance"):
            if a.get("asset") == "USDT":
                return {"balance": float(a["balance"]), "available": float(a["availableBalance"])}
        return {"balance": 0.0, "available": 0.0}

    async def position(self, symbol: str) -> dict:
        rows = await self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol.upper()})
        amt = sum(float(r["positionAmt"]) for r in rows)
        r0 = rows[0] if rows else {}
        return {"amt": amt, "entry": float(r0.get("entryPrice", 0) or 0), "mark": float(r0.get("markPrice", 0) or 0),
                "unrealized": sum(float(r.get("unRealizedProfit", 0) or 0) for r in rows)}

    async def realized_since(self, symbol: str, since_ms: int) -> dict:
        """Realised P&L, fees and funding for one symbol since a time, from the income history."""
        rows = await self._request("GET", "/fapi/v1/income", {"symbol": symbol.upper(), "startTime": since_ms, "limit": 1000})
        out = {"realized": 0.0, "fees": 0.0, "funding": 0.0}
        for r in rows:
            v = float(r.get("income", 0) or 0)
            t = r.get("incomeType")
            if t == "REALIZED_PNL":
                out["realized"] += v
            elif t == "COMMISSION":
                out["fees"] += v
            elif t == "FUNDING_FEE":
                out["funding"] += v
        out["net"] = out["realized"] + out["fees"] + out["funding"]
        return out

    # ── writes ─────────────────────────────────────────────────
    async def prepare(self, symbol: str, leverage: int) -> None:
        try:
            await self._request("POST", "/fapi/v1/marginType", {"symbol": symbol.upper(), "marginType": "ISOLATED"})
        except BinanceError as e:
            if e.code != -4046:                       # "No need to change margin type"
                raise
        await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol.upper(), "leverage": int(leverage)})

    async def market(self, symbol: str, side: str, qty: float, reduce_only: bool = False, client_id: str | None = None) -> dict:
        body = await self._request("POST", "/fapi/v1/order", {
            "symbol": symbol.upper(), "side": side, "type": "MARKET", "quantity": f"{qty}",
            "reduceOnly": "true" if reduce_only else None, "newClientOrderId": client_id, "newOrderRespType": "RESULT"})
        return {"order_id": body.get("orderId"), "avg_price": float(body.get("avgPrice") or 0),
                "qty": float(body.get("executedQty") or 0), "status": body.get("status")}

    async def protect(self, symbol: str, close_side: str, kind: str, trigger: float) -> dict:
        """Server-side stop or target that closes the whole position at the mark price. kind: STOP_MARKET or
        TAKE_PROFIT_MARKET."""
        params = {"symbol": symbol.upper(), "side": close_side, "type": kind, "stopPrice": f"{trigger}",
                  "closePosition": "true", "workingType": "MARK_PRICE", "priceProtect": "true"}
        try:
            body = await self._request("POST", "/fapi/v1/order", params)
            return {"id": body.get("orderId"), "via": "order"}
        except BinanceError as e:
            if e.code not in (-4120, -1116, -4164):    # endpoint refuses conditional orders: use the algo endpoint
                raise
        algo = {"algoType": "CONDITIONAL", "symbol": symbol.upper(), "side": close_side, "type": kind,
                "triggerPrice": f"{trigger}", "closePosition": "true", "workingType": "MARK_PRICE", "priceProtect": "true"}
        body = await self._request("POST", "/fapi/v1/algoOrder", algo)
        return {"id": body.get("algoId"), "via": "algo"}

    async def cancel_all(self, symbol: str) -> None:
        await self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol.upper()})
        try:
            await self._request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol.upper()})
        except BinanceError:
            pass                                       # no algo orders, or endpoint not on this venue

    async def open_protection(self, symbol: str) -> int:
        """How many stop/target orders guard this symbol (classic + algo)."""
        n = sum(1 for o in await self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol.upper()})
                if o.get("type") in ("STOP_MARKET", "TAKE_PROFIT_MARKET"))
        try:
            algo = await self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol.upper()})
            rows = algo.get("orders", algo) if isinstance(algo, dict) else algo
            n += sum(1 for o in rows if o.get("orderType", o.get("type")) in ("STOP_MARKET", "TAKE_PROFIT_MARKET"))
        except BinanceError:
            pass
        return n
