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

import asyncio
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from execution.delta_india import DeltaError, DeltaIndia, Product, binance_to_delta_symbol, round_to_tick

SHADOW_PATH = Path("data/delta_shadow.jsonl")
MIN_STOP_FRAC = 0.005        # a stop closer than 0.5% is noise, and sizes absurdly large
MAX_LEVERAGE = 10.0          # sizing never needs more margin than the wallet has at this leverage
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
                 shadow_path: Path = SHADOW_PATH, state_path: Path = STATE_PATH,
                 kill_switch: bool = False, backstop_r: float = 0.5,
                 daily_loss_pct: float = 0.0, min_wallet_inr: float = 0.0):
        self.client = client
        self.mode = mode
        self.live_orders = live_orders
        self.risk_pct = risk_pct
        self.max_open = max_open
        self.usd_inr = usd_inr
        self.shadow_path = shadow_path
        self.state_path = state_path
        self.kill_switch = kill_switch
        self.backstop_r = backstop_r
        self.daily_loss_pct = daily_loss_pct      # no new orders once the wallet is this far below today's start
        self.min_wallet_inr = min_wallet_inr      # no new orders at all once the wallet is below this
        self.retry_delay_s = 1.0

    @property
    def live(self) -> bool:
        return self.mode == "live" and self.live_orders

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
            if hasattr(self.client, "wallet_inr"):
                inr, rate = await self.client.wallet_inr()
                if rate:
                    self.usd_inr = rate                     # size with the exchange's own conversion rate
            else:
                inr = await self.client.inr_available()
        except DeltaError as e:
            return MirrorPlan(False, f"wallet_{e}", self.mode, binance_symbol, delta_sym)
        side = "buy" if side_word.lower() in ("long", "buy") else "sell"
        # Shadow with an empty INR wallet still sizes against a ₹5,000 notional so the
        # log shows a realistic order; live mode always uses the real balance.
        sizing_equity = inr if self.mode == "live" else max(inr, 5000.0)
        if entry > 0 and abs(entry - stop) / entry < MIN_STOP_FRAC:
            return MirrorPlan(False, "stop_too_close", self.mode, binance_symbol, delta_sym, product.id, side,
                              0, entry, stop, target, 0.0, 0.0, inr)
        size, notional, risk = size_contracts(
            product, entry, stop, sizing_equity, self.risk_pct, self.usd_inr)
        # never ask for more than the wallet can margin (testnet 14 Oct: a 0.3% stop asked for 1,491 contracts)
        max_notional_usd = (sizing_equity / self.usd_inr) * MAX_LEVERAGE * 0.9 if self.usd_inr else 0.0
        if size and product.contract_value * entry > 0:
            cap = int(max_notional_usd // (product.contract_value * entry))
            if size > cap:
                size = cap
                notional = size * product.contract_value * entry
                risk = notional * abs(entry - stop) / entry * self.usd_inr
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

    async def _loss_guard(self) -> str:
        """'' when new orders may go out, else the reason they may not. The day's starting wallet (UTC) is stored
        in the state file; an unreadable wallet blocks new orders (fail closed) but never touches open ones."""
        if self.daily_loss_pct <= 0 and self.min_wallet_inr <= 0:
            return ""
        try:
            if hasattr(self.client, "wallet_total_inr"):
                wallet = await self.client.wallet_total_inr()
            else:
                wallet = await self.client.inr_available()
        except DeltaError:
            return "wallet_unreadable"
        if self.min_wallet_inr > 0 and wallet < self.min_wallet_inr:
            return "wallet_floor"
        if self.daily_loss_pct > 0:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            state = self._load_state()
            day = state.get("day") or {}
            if day.get("date") != today or not day.get("start_inr"):
                state["day"] = day = {"date": today, "start_inr": wallet}
                self._save_state(state)
            if wallet < float(day["start_inr"]) * (1 - self.daily_loss_pct):
                return "daily_loss_limit"
        return ""

    async def on_paper_open(self, binance_symbol: str, side_word: str, entry: float,
                            stop: float, target: float, signal_type: str = "") -> MirrorPlan:
        """Called after a paper swing position opens."""
        if self.mode == "off":
            return MirrorPlan(False, "mode_off")
        if self.kill_switch:
            self._append_shadow({"t": int(time.time() * 1000), "event": "refused", "reason": "kill_switch",
                                 "binance_symbol": binance_symbol})
            return MirrorPlan(False, "kill_switch")
        guard = await self._loss_guard() if self.live else ""
        if guard:
            self._append_shadow({"t": int(time.time() * 1000), "event": "refused", "reason": guard,
                                 "binance_symbol": binance_symbol})
            return MirrorPlan(False, guard)
        plan = await self.plan(binance_symbol, side_word, entry, stop, target)
        row = {**asdict(plan), "t": int(time.time() * 1000), "signal_type": signal_type,
               "placed": False}
        if not plan.ok:
            self._append_shadow(row)
            return plan

        product = await self.client.product(plan.delta_symbol)
        tick = product.tick_size if product else 0.0
        record = {"side": plan.side, "size": plan.size, "entry": plan.entry, "stop": plan.stop,
                  "init_stop": plan.stop, "exchange_stop": plan.stop, "risk": abs(plan.entry - plan.stop),
                  "target": plan.target, "product_id": plan.product_id, "tick": tick,
                  "binance_symbol": binance_symbol.lower(), "opened_ms": int(time.time() * 1000),
                  "signal_type": signal_type}
        can_live = self.live
        if not can_live:
            row["note"] = "shadow_only_no_order"
            self._append_shadow(row)
            state = self._load_state()                       # rehearse the ladder in shadow too
            state.setdefault("shadow", {})[plan.delta_symbol] = record
            self._save_state(state)
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
            record.update(stop_order_id=_oid(stop_order), tp_order_id=_oid(tp_order),
                          entry_fill=(entry_order or {}).get("average_fill_price"))
            state.setdefault("positions", {})[plan.delta_symbol] = record
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


    # --- the profit ladder on the exchange (13 Oct 2026) -------------------------------------------------------------

    def backstop_for(self, side: int, init_stop: float, risk: float, lock_stop: float) -> float:
        """Exchange stop for an engine lock: backstop_r R behind it, never looser than the original stop."""
        cand = lock_stop - side * self.backstop_r * risk
        return max(init_stop, cand) if side > 0 else min(init_stop, cand)

    async def on_stop_change(self, binance_symbol: str, lock_stop: float) -> dict:
        """The engine moved this trade's stop (ladder). Tighten the exchange backstop if it should move."""
        delta_sym = binance_to_delta_symbol(binance_symbol)
        state = self._load_state()
        where = "positions" if self.live else "shadow"
        pos = (state.get(where) or {}).get(delta_sym)
        if not pos:
            return {"ok": False, "reason": "not_open_on_delta"}
        side = 1 if pos["side"] == "buy" else -1
        want = round_to_tick(self.backstop_for(side, pos["init_stop"], pos["risk"], lock_stop), pos.get("tick", 0), side)
        cur = pos["exchange_stop"]
        tighter = want > cur if side > 0 else want < cur
        if not tighter or abs(want - cur) < max(pos.get("tick", 0), 1e-12) / 2:
            return {"ok": True, "reason": "unchanged", "exchange_stop": cur}
        row = {"t": int(time.time() * 1000), "event": "amend_stop", "delta_symbol": delta_sym, "from": cur, "to": want,
               "engine_lock": lock_stop, "mode": self.mode, "placed": False}
        if self.live:
            try:
                try:
                    await self.client.edit_stop(pos["stop_order_id"], pos["product_id"], pos["size"], want)
                except DeltaError as first:
                    row["edit_error"] = str(first)[:160]
                    await asyncio.sleep(self.retry_delay_s)            # testnet 14 Oct: an amend right after the
                    try:                                                # stop was created failed once, then worked
                        await self.client.edit_stop(pos["stop_order_id"], pos["product_id"], pos["size"], want)
                    except DeltaError:
                        # fallback: place the NEW stop first, then cancel the old one. Both are reduce-only, so two
                        # stops for a moment is harmless; zero stops (the old cancel-first order) was not.
                        close_side = "sell" if side > 0 else "buy"
                        new = await self.client.place_stop_market(pos["product_id"], close_side, pos["size"], want)
                        old_id, pos["stop_order_id"] = pos["stop_order_id"], _oid(new)
                        try:
                            await self.client.cancel_order(old_id, pos["product_id"])
                        except DeltaError as e:
                            row["old_stop_cancel_error"] = str(e)[:160]
                row["placed"] = True
            except DeltaError as e:
                row["error"] = str(e)[:200]
                if "immediate_execution_stop_order" in str(e):
                    # price is already beyond where the backstop belongs (testnet 14 Oct): do what that stop
                    # would have done and close at market; the old stop stays until the close is through
                    self._append_shadow(row)
                    closed = await self.on_paper_close(binance_symbol, "backstop_breached")
                    return {"ok": closed.get("ok", False), "reason": "closed_backstop_breached"}
                self._append_shadow(row)
                return {"ok": False, "reason": f"amend_failed_{e}"}
        pos["exchange_stop"] = want
        state[where][delta_sym] = pos
        self._save_state(state)
        self._append_shadow(row)
        return {"ok": True, "reason": "amended", "exchange_stop": want}

    async def on_paper_close(self, binance_symbol: str, reason: str = "") -> dict:
        """The engine closed this trade (target, confirmed lock, time, stop). Flatten on Delta, cancel leftovers."""
        delta_sym = binance_to_delta_symbol(binance_symbol)
        state = self._load_state()
        where = "positions" if self.live else "shadow"
        pos = (state.get(where) or {}).pop(delta_sym, None)
        if not pos:
            return {"ok": False, "reason": "not_open_on_delta"}
        row = {"t": int(time.time() * 1000), "event": "close", "delta_symbol": delta_sym, "reason": reason,
               "mode": self.mode, "placed": False}
        if self.live:
            try:
                size = await self.client.position_size(pos["product_id"])
                if size:
                    await self.client.place_market(pos["product_id"], "sell" if size > 0 else "buy", abs(size),
                                                   reduce_only=True)
                    row["closed_contracts"] = abs(size)
                await self.client.cancel_all(pos["product_id"])
                row["placed"] = True
            except DeltaError as e:
                row["error"] = str(e)[:200]
                state[where][delta_sym] = pos          # keep it so reconcile retries
                self._save_state(state)
                self._append_shadow(row)
                return {"ok": False, "reason": f"close_failed_{e}"}
        self._save_state(state)
        self._append_shadow(row)
        return {"ok": True, "reason": "closed"}

    async def reconcile(self) -> list[dict]:
        """Compare our live records with Delta's positions. A position Delta closed itself (backstop or target hit)
        is removed and its leftover orders cancelled; a size mismatch is reported."""
        if not self.live:
            return []
        await self._loss_guard()               # rolls the day's starting wallet even on days with no new trade
        state = self._load_state()
        events = []
        for delta_sym, pos in list((state.get("positions") or {}).items()):
            try:
                size = await self.client.position_size(pos["product_id"])
            except DeltaError:
                # one retry: the demo answered {'code': 'unavailable'} for a single run on 15 Oct 12:25 UTC
                await asyncio.sleep(self.retry_delay_s * 2)
                try:
                    size = await self.client.position_size(pos["product_id"])
                except DeltaError as e:
                    events.append({"delta_symbol": delta_sym, "event": "reconcile_error", "error": str(e)[:160]})
                    continue
            if size == 0:
                try:
                    await self.client.cancel_all(pos["product_id"])
                except DeltaError:
                    pass
                state["positions"].pop(delta_sym, None)
                events.append({"delta_symbol": delta_sym, "event": "closed_on_exchange",
                               "binance_symbol": pos.get("binance_symbol")})
            else:
                if abs(size) != int(pos["size"]):
                    events.append({"delta_symbol": delta_sym, "event": "size_mismatch", "ours": pos["size"],
                                   "delta": size})
                # every open position must have a stop on the exchange; put one back if it is missing
                try:
                    orders = await self.client.open_orders(pos["product_id"])
                    if not any(o.get("stop_order_type") == "stop_loss_order" for o in orders):
                        side = 1 if pos["side"] == "buy" else -1
                        new = await self.client.place_stop_market(pos["product_id"], "sell" if side > 0 else "buy",
                                                                  abs(size), pos["exchange_stop"])
                        pos["stop_order_id"] = _oid(new)
                        events.append({"delta_symbol": delta_sym, "event": "stop_restored",
                                       "stop": pos["exchange_stop"]})
                except DeltaError as e:
                    events.append({"delta_symbol": delta_sym, "event": "stop_check_error", "error": str(e)[:160]})
        self._save_state(state)
        for e in events:
            self._append_shadow({"t": int(time.time() * 1000), **e})
        return events

    async def flatten_all(self) -> list[dict]:
        """Kill switch action: close every live Delta position and cancel its orders."""
        out = []
        for delta_sym, pos in list((self._load_state().get("positions") or {}).items()):
            out.append({delta_sym: await self.on_paper_close(pos.get("binance_symbol") or delta_sym, "kill_switch")})
        return out


def _oid(obj) -> int | None:
    if isinstance(obj, dict):
        for k in ("id", "order_id"):
            if obj.get(k) is not None:
                try:
                    return int(obj[k])
                except (TypeError, ValueError):
                    pass
    return None


def _trim(obj) -> dict:
    if not isinstance(obj, dict):
        return {"raw": str(obj)[:200]}
    keep = ("id", "order_id", "product_id", "side", "size", "state", "average_fill_price",
            "limit_price", "stop_price", "order_type")
    return {k: obj[k] for k in keep if k in obj}
