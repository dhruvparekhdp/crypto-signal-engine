"""
Delta Exchange India REST client (INR-settled futures/options).

Auth: HMAC-SHA256 over  method + timestamp + path + query_string + body
Base: https://api.india.delta.exchange  (NOT api.delta.exchange — that is Global)

Keys come from settings / the /keys page. Never logged. Never returned by any endpoint.
User-Agent is required by Delta; without it requests get 4xx.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

INDIA_URL = "https://api.india.delta.exchange"
INDIA_TESTNET_URL = "https://cdn-ind.testnet.deltaex.org"
USER_AGENT = "crypto-signal-engine/delta-india"


class DeltaError(RuntimeError):
    def __init__(self, status: int, body: Any):
        msg = body.get("error") if isinstance(body, dict) else str(body)
        detail = body.get("message") if isinstance(body, dict) else ""
        super().__init__(f"delta {status}: {msg} {detail}".strip())
        self.status, self.body = status, body


def sign(secret: str, method: str, timestamp: str, path: str,
         query_string: str = "", body: str = "") -> str:
    """Delta signature. query_string must include the leading '?' when non-empty."""
    prehash = f"{method.upper()}{timestamp}{path}{query_string}{body}"
    return hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()


def binance_to_delta_symbol(binance_symbol: str) -> str:
    """XRPUSDT -> XRPUSD (Delta India perpetual naming)."""
    s = (binance_symbol or "").upper().strip()
    if s.endswith("USDT"):
        return s[:-4] + "USD"
    if s.endswith("USD"):
        return s
    return s + "USD"


@dataclass(frozen=True)
class Product:
    id: int
    symbol: str
    contract_value: float
    tick_size: float
    initial_margin_pct: float   # e.g. 1.0 means 1% -> up to 100x; we never max it
    underlying: str


class DeltaIndia:
    def __init__(self, api_key: str, api_secret: str, base_url: str = INDIA_URL, client=None):
        self._key = api_key
        self._secret = api_secret
        self.base = base_url.rstrip("/")
        self._client = client
        self._products: dict[str, Product] = {}
        self._products_at = 0.0

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       body: dict | None = None, signed: bool = True) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        query_string = ("?" + urlencode(params)) if params else ""
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if signed:
            ts = str(int(time.time()))
            headers.update({
                "api-key": self._key,
                "timestamp": ts,
                "signature": sign(self._secret, method, ts, path, query_string, payload),
            })
        url = f"{self.base}{path}{query_string}"
        r = await self._client.request(
            method.upper(), url,
            content=payload.encode() if payload else None,
            headers=headers, timeout=15,
        )
        try:
            data = r.json()
        except ValueError:
            data = {"error": r.text[:200]}
        if r.status_code >= 400 or (isinstance(data, dict) and data.get("success") is False):
            raise DeltaError(r.status_code, data)
        return data.get("result", data) if isinstance(data, dict) else data

    async def ping(self) -> dict:
        """Read-only auth check: wallet balances. Proves key + IP whitelist."""
        bals = await self._request("GET", "/v2/wallet/balances")
        return {"ok": True, "balances": bals}

    async def balances(self) -> list[dict]:
        got = await self._request("GET", "/v2/wallet/balances")
        return got if isinstance(got, list) else (got.get("balances") or got or [])

    async def inr_available(self) -> float:
        """Available INR balance (asset_symbol INR / available_balance)."""
        for b in await self.balances():
            sym = (b.get("asset_symbol") or b.get("symbol") or "").upper()
            if sym in ("INR", "RUPEE"):
                for k in ("available_balance", "available_balance_inr", "balance", "available"):
                    if b.get(k) is not None:
                        try:
                            return float(b[k])
                        except (TypeError, ValueError):
                            pass
        return 0.0

    async def positions(self) -> list[dict]:
        got = await self._request("GET", "/v2/positions")
        return got if isinstance(got, list) else []

    async def product(self, symbol: str) -> Product | None:
        await self._ensure_products()
        return self._products.get(symbol.upper())

    async def _ensure_products(self) -> None:
        if self._products and time.time() - self._products_at < 600:
            return
        self._products = await self._load_products()
        self._products_at = time.time()

    async def _load_products(self) -> dict[str, Product]:
        after = None
        out: dict[str, Product] = {}
        for _ in range(20):
            params: dict[str, Any] = {
                "contract_types": "perpetual_futures", "states": "live", "page_size": 50,
            }
            if after:
                params["after"] = after
            r = await self._client.get(
                f"{self.base}/v2/products", params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=20)
            data = r.json()
            if r.status_code >= 400:
                raise DeltaError(r.status_code, data)
            rows = data.get("result") or []
            for p in rows:
                u = p.get("underlying_asset") or {}
                out[p["symbol"].upper()] = Product(
                    id=int(p["id"]),
                    symbol=p["symbol"].upper(),
                    contract_value=float(p.get("contract_value") or 1),
                    tick_size=float(p.get("tick_size") or 0.01),
                    initial_margin_pct=float(p.get("initial_margin") or 1),
                    underlying=((u.get("symbol") if isinstance(u, dict) else None)
                                or p["symbol"][:-3]),
                )
            after = (data.get("meta") or {}).get("after")
            if not after or not rows:
                break
        return out

    async def place_market(self, product_id: int, side: str, size: int,
                           reduce_only: bool = False) -> dict:
        """side: buy|sell. size: integer contracts. Requires Trading permission on the key."""
        body = {
            "product_id": product_id,
            "size": int(size),
            "side": side.lower(),
            "order_type": "market_order",
            "reduce_only": bool(reduce_only),
        }
        return await self._request("POST", "/v2/orders", body=body)

    async def place_stop_market(self, product_id: int, side: str, size: int,
                                stop_price: float) -> dict:
        body = {
            "product_id": product_id,
            "size": int(size),
            "side": side.lower(),
            "order_type": "market_order",
            "stop_order_type": "stop_loss_order",
            "stop_price": f"{stop_price:.8f}".rstrip("0").rstrip("."),
            "reduce_only": True,
        }
        return await self._request("POST", "/v2/orders", body=body)

    async def place_take_profit_market(self, product_id: int, side: str, size: int,
                                       stop_price: float) -> dict:
        body = {
            "product_id": product_id,
            "size": int(size),
            "side": side.lower(),
            "order_type": "market_order",
            "stop_order_type": "take_profit_order",
            "stop_price": f"{stop_price:.8f}".rstrip("0").rstrip("."),
            "reduce_only": True,
        }
        return await self._request("POST", "/v2/orders", body=body)

    async def cancel_all(self, product_id: int) -> dict:
        return await self._request("DELETE", "/v2/orders/all", params={"product_id": product_id})
