"""
Mirror paper swing entries onto Delta Exchange India (INR-settled perpetuals).

Modes (settings.delta_india_mode):
  off     — do nothing
  shadow  — resolve product, size the order, append to data/delta_shadow.jsonl;
            NEVER place an order. Works with a Read-only API key.
  live    — place market + stop + take-profit on Delta. Requires:
              * delta_india_mode == "live"
              * delta_india_live_orders == True   (second gate)
              * API key with Trading permission + IP whitelist

Paper book stays the source of signals. This module never opens a Delta trade
on its own — only when called after a paper swing open.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from execution.delta_india import DeltaError, DeltaIndia, Product, binance_to_delta_symbol

SHADOW_PATH = Path("data/delta_shadow.jsonl")
STATE_PATH = Path("data/delta_state.json")


@dataclass
class MirrorPlan:
    ok: bool
    reason: str = ""
    mode: str = "shadow"
    binance_symbol: str = ""
    delta_symbol: str = ""
    product_id: int = 0
    side: str = ""            # buy | sell
    size: int = 0             # contracts
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    notional_usd: float = 0.0
    risk_inr: float = 0.0
    inr_available: float = 0.0


def size_contracts(product: Product, entry: float, stop: float, equity_inr: float,
                   risk_pct: float, usd_inr: float) -> tuple[int, float, float]:
    """Return (contracts, notional_usd, risk_inr)."""
    if entry <= 0 or equity_inr <= 0 or usd_inr <= 0:
        return 0, 0.0, 0.0
    stop_frac = abs(entry - stop) / entry
    if stop_frac <= 0:
        return 0, 0.0, 0.0
    risk_inr = equity_inr * risk_pct
    # notional in USD such that a stop-out costs risk_inr
    # loss_usd = notional_usd * stop_frac  => notional_usd = risk_inr / usd_inr / stop_frac
    notional_usd = (risk_inr / usd_inr) / stop_frac
    coin_qty = notional_usd / entry
    contracts = int(math.floor(coin_qty / product.contract_value + 1e-12))
    if contracts < 1:
        return 0, 0.0, 0.0
    notional_usd = contracts * product.contract_value * entry
    risk_inr = notional_usd * stop_frac * usd_inr
    return contracts, notional_usd, risk_inr


class DeltaBook:
    def __init__(self, client: DeltaIndia, *, mode: str = "shadow",
                 live_orders: bool = False, risk_pct: float = 0.01,
                 max_open: int = 3, usd_inr: float = 83.0,
                 shadow_path: Path = SHADOW_PATH, state_path: Path = STATE_PATH):
        self.client = client
        self.mode = mode
        self.live_orders = live_orders
        self.risk_pct = risk_pct
        self.max_open = max_open
        self.usd_inr = usd_inr
        self.shadow_path = shadow_path
        self.state_path = state_path

    def _append_shadow(self, row: dict) -> None:
        self.shadow_path.parent.mkdir(parents=True, exist_ok=True)
        with self.shadow_path.open("a") as f:
            f.write(json.dumps(row) + "\n")

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {"positions": {}, "errors": []}

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(self.state_path)

    async def plan(self, binance_symbol: str, side_word: str, entry: float,
                   stop: float, target: float) -> MirrorPlan:
        delta_sym = binance_to_delta_symbol(binance_symbol)
        product = await self.client.product(delta_sym)
        if product is None:
            return MirrorPlan(False, f"no_delta_product_{delta_sym}", self.mode,
                              binance_symbol, delta_sym)
        try:
            inr = await self.client.inr_available()
        except DeltaError as e:
            return MirrorPlan(False, f"wallet_{e}", self.mode, binance_symbol, delta_sym)
        side = "buy" if side_word.lower() in ("long", "buy") else "sell"
        # Shadow with an empty INR wallet still sizes against a ₹5,000 notional so the
        # log shows a realistic order; live mode always uses the real balance.
        sizing_equity = inr if self.mode == "live" else max(inr, 5000.0)
        size, notional, risk = size_contracts(
            product, entry, stop, sizing_equity, self.risk_pct, self.usd_inr)
        if size < 1:
            return MirrorPlan(False, "below_one_contract", self.mode, binance_symbol, delta_sym,
                              product.id, side, 0, entry, stop, target, 0.0, 0.0, inr)
        state = self._load_state()
        if self.max_open and len(state.get("positions") or {}) >= self.max_open:
            return MirrorPlan(False, "delta_book_full", self.mode, binance_symbol, delta_sym,
                              product.id, side, size, entry, stop, target, notional, risk, inr)
        if delta_sym in (state.get("positions") or {}):
            return MirrorPlan(False, "already_open_on_delta", self.mode, binance_symbol, delta_sym,
                              product.id, side, size, entry, stop, target, notional, risk, inr)
        return MirrorPlan(True, "", self.mode, binance_symbol, delta_sym, product.id, side,
                          size, entry, stop, target, notional, risk, inr)

    async def on_paper_open(self, binance_symbol: str, side_word: str, entry: float,
                            stop: float, target: float, signal_type: str = "") -> MirrorPlan:
        """Called after a paper swing position opens."""
        if self.mode == "off":
            return MirrorPlan(False, "mode_off")
        plan = await self.plan(binance_symbol, side_word, entry, stop, target)
        row = {**asdict(plan), "t": int(time.time() * 1000), "signal_type": signal_type,
               "placed": False}
        if not plan.ok:
            self._append_shadow(row)
            return plan

        can_live = (self.mode == "live" and self.live_orders)
        if not can_live:
            row["note"] = "shadow_only_no_order"
            self._append_shadow(row)
            return plan

        # LIVE path — real orders
        try:
            entry_order = await self.client.place_market(plan.product_id, plan.side, plan.size)
            close_side = "sell" if plan.side == "buy" else "buy"
            stop_order = await self.client.place_stop_market(
                plan.product_id, close_side, plan.size, plan.stop)
            tp_order = await self.client.place_take_profit_market(
                plan.product_id, close_side, plan.size, plan.target)
            row.update({
                "placed": True,
                "entry_order": _trim(entry_order),
                "stop_order": _trim(stop_order),
                "tp_order": _trim(tp_order),
            })
            state = self._load_state()
            state.setdefault("positions", {})[plan.delta_symbol] = {
                "side": plan.side, "size": plan.size, "entry": plan.entry,
                "stop": plan.stop, "target": plan.target, "product_id": plan.product_id,
                "opened_ms": int(time.time() * 1000), "signal_type": signal_type,
            }
            self._save_state(state)
        except DeltaError as e:
            row["placed"] = False
            row["error"] = str(e)[:200]
            state = self._load_state()
            state.setdefault("errors", []).append({
                "t": int(time.time() * 1000),
                "error": str(e)[:200],
                "symbol": plan.delta_symbol,
            })
            self._save_state(state)
            plan = MirrorPlan(False, f"order_failed_{e}", plan.mode, plan.binance_symbol,
                              plan.delta_symbol, plan.product_id, plan.side, plan.size,
                              plan.entry, plan.stop, plan.target, plan.notional_usd,
                              plan.risk_inr, plan.inr_available)
        self._append_shadow(row)
        return plan


def _trim(obj) -> dict:
    if not isinstance(obj, dict):
        return {"raw": str(obj)[:200]}
    keep = ("id", "order_id", "product_id", "side", "size", "state", "average_fill_price",
            "limit_price", "stop_price", "order_type")
    return {k: obj[k] for k in keep if k in obj}
