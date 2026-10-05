"""
Live swing book: copies the paper swing book's entries to a small real (or testnet) Binance futures account.

Same signal, same stop distance, same 3R target, same 7-day limit as the paper trade. What differs is money:

  * the account is treated as at most `live_max_balance_usdt`, whatever it actually holds
  * no limit on open positions unless `live_max_open` is set; free margin and leverage decide what fits
  * risk per trade `live_risk_pct`; when Binance's minimum order forces a bigger trade, it is allowed up to
    `live_max_risk_pct` and refused above it (that is why BTC and ETH cannot trade on a $20 account)
  * no new entries after the day's losses reach `live_daily_loss_pct` of the capped balance
  * stop and target sit on the exchange (closePosition at mark price), so they work even if this server is down
  * kill switch: closes everything and blocks new entries until switched back

State lives in a small JSON file on the server (data/live_state.json) with the exchange as the source of truth;
every finished trade is appended to data/live_trades.jsonl.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from execution.binance_futures import BinanceError, SymbolRules

DAY_MS = 86_400_000


@dataclass
class EntryPlan:
    ok: bool
    reason: str = ""
    qty: float = 0.0
    notional: float = 0.0
    leverage: int = 1
    risk_usdt: float = 0.0
    risk_pct: float = 0.0


def plan_entry(balance: float, available: float, cap: float, risk_pct: float, max_risk_pct: float, price: float,
               stop: float, rules: SymbolRules, max_leverage: int) -> EntryPlan:
    """Size one live entry. `balance`/`available` are the account's; the plan never uses more than `cap`."""
    equity = min(balance, cap)
    if equity <= 0 or price <= 0:
        return EntryPlan(False, "no_balance")
    stop_frac = abs(price - stop) / price
    if stop_frac <= 0:
        return EntryPlan(False, "bad_stop")
    notional = equity * risk_pct / stop_frac
    floor = max(rules.min_notional * 1.02, rules.min_qty * price)          # 2% above the minimum so rounding stays above it
    if notional < floor:
        notional = floor
    qty = rules.floor_qty(notional / price)
    if qty * price < rules.min_notional:
        qty = rules.floor_qty(qty + rules.step)
    notional = qty * price
    risk = notional * stop_frac
    if qty <= 0:
        return EntryPlan(False, "below_one_lot")
    if risk > equity * max_risk_pct + 1e-9:
        return EntryPlan(False, f"min_order_risks_{risk / equity * 100:.1f}pct", qty, notional, 0, risk, risk / equity)
    usable = min(available, cap) * 0.9
    if usable <= 0:
        return EntryPlan(False, "no_free_margin")
    lev = max(1, math.ceil(notional / usable))
    if lev > max_leverage:
        return EntryPlan(False, f"needs_{lev}x_leverage", qty, notional, lev, risk, risk / equity)
    return EntryPlan(True, "", qty, notional, lev, risk, risk / equity)


@dataclass
class LivePosition:
    symbol: str
    side: str                 # "long" | "short"
    qty: float
    entry: float
    stop: float
    target: float
    opened_ms: int
    expires_ms: int
    signal_type: str
    paper_entry: float        # the paper book's entry price, for slippage
    leverage: int
    risk_usdt: float
    protection: list = field(default_factory=list)


class LiveState:
    def __init__(self, path: str = "data/live_state.json", trades_path: str = "data/live_trades.jsonl"):
        self.path, self.trades_path = Path(path), Path(trades_path)
        self.positions: dict[str, LivePosition] = {}
        self.killed = False
        self.errors: list[dict] = []
        self.skips: list[dict] = []
        self.load()

    def load(self) -> None:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.positions = {k: LivePosition(**v) for k, v in d.get("positions", {}).items()}
        self.killed = bool(d.get("killed", False))
        self.errors = d.get("errors", [])[-50:]
        self.skips = d.get("skips", [])[-50:]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"positions": {k: asdict(v) for k, v in self.positions.items()}, "killed": self.killed,
                                   "errors": self.errors[-50:], "skips": self.skips[-50:]}))
        tmp.replace(self.path)

    def note(self, kind: str, **kw) -> None:
        (self.errors if kind == "error" else self.skips).append({"t": int(time.time() * 1000), **kw})
        self.save()

    def record_trade(self, row: dict) -> None:
        self.trades_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.trades_path, "a") as f:
            f.write(json.dumps(row) + "\n")

    def trades(self, limit: int = 200) -> list[dict]:
        try:
            lines = self.trades_path.read_text().splitlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def loss_today(self, now_ms: int) -> float:
        day0 = now_ms - now_ms % DAY_MS
        return -sum(t.get("net", 0.0) for t in self.trades(500) if t.get("closed_ms", 0) >= day0 and t.get("net", 0) < 0)


class LiveBook:
    """Opens, guards and closes live positions. All money settings come from `cfg` (the app settings)."""

    def __init__(self, client, state: LiveState, cfg):
        self.client, self.state, self.cfg = client, state, cfg

    def _skip(self, symbol, reason, **kw) -> None:
        self.state.note("skip", symbol=symbol, reason=reason, **kw)
        return None

    async def open_from_paper(self, symbol: str, side: str, paper_entry: float, stop_dist: float, signal_type: str,
                              now_ms: int) -> LivePosition | None:
        cfg, st = self.cfg, self.state
        sym = symbol.upper()
        if st.killed:
            return self._skip(sym, "kill_switch_on")
        if cfg.live_max_open and len(st.positions) >= cfg.live_max_open:
            return self._skip(sym, "live_book_full")
        if sym in st.positions:
            return self._skip(sym, "already_open")
        cap = cfg.live_max_balance_usdt
        if st.loss_today(now_ms) >= cfg.live_daily_loss_pct * cap:
            return self._skip(sym, "daily_loss_limit")
        try:
            rules = await self.client.rules(sym)
            bal = await self.client.usdt_balance()
            pos = await self.client.position(sym)
            if abs(pos["amt"]) > 0:
                return self._skip(sym, "exchange_already_has_position")
            sgn = 1 if side == "long" else -1
            plan = plan_entry(bal["balance"], bal["available"], cap, cfg.live_risk_pct, cfg.live_max_risk_pct,
                              paper_entry, paper_entry - sgn * stop_dist, rules, cfg.live_max_leverage)
            if not plan.ok:
                return self._skip(sym, plan.reason, notional=round(plan.notional, 2), risk=round(plan.risk_usdt, 3))
            await self.client.prepare(sym, plan.leverage)
            fill = await self.client.market(sym, "BUY" if sgn > 0 else "SELL", plan.qty,
                                            client_id=f"swing{now_ms // 1000}{sym[:4].lower()}")
            entry = fill["avg_price"] or paper_entry
            qty = fill["qty"] or plan.qty
            stop = rules.round_price(entry - sgn * stop_dist)
            target = rules.round_price(entry + sgn * 3.0 * stop_dist)
            close_side = "SELL" if sgn > 0 else "BUY"
            lp = LivePosition(sym, side, qty, entry, stop, target, now_ms, now_ms + cfg.swing_hold_minutes * 60_000,
                              signal_type, paper_entry, plan.leverage, plan.risk_usdt)
            st.positions[sym] = lp
            st.save()
            try:
                lp.protection.append(await self.client.protect(sym, close_side, "STOP_MARKET", stop))
                lp.protection.append(await self.client.protect(sym, close_side, "TAKE_PROFIT_MARKET", target))
            except BinanceError as e:
                # never leave an unprotected position: close it straight away
                st.note("error", symbol=sym, where="protect", msg=str(e))
                await self.close(sym, now_ms, reason="protection_failed")
                return None
            st.save()
            return lp
        except BinanceError as e:
            st.note("error", symbol=sym, where="open", msg=str(e))
            return None

    async def close(self, sym: str, now_ms: int, reason: str) -> None:
        lp = self.state.positions.get(sym)
        try:
            pos = await self.client.position(sym)
            if abs(pos["amt"]) > 0:
                await self.client.market(sym, "SELL" if pos["amt"] > 0 else "BUY", abs(pos["amt"]), reduce_only=True)
            await self.client.cancel_all(sym)
        except BinanceError as e:
            self.state.note("error", symbol=sym, where="close", msg=str(e))
            return
        if lp is not None:
            await self._finish(lp, now_ms, reason)

    async def _finish(self, lp: LivePosition, now_ms: int, reason: str) -> None:
        try:
            pnl = await self.client.realized_since(lp.symbol, lp.opened_ms - 60_000)
        except BinanceError:
            pnl = {"realized": None, "fees": None, "funding": None, "net": None}
        sgn = 1 if lp.side == "long" else -1
        slip = sgn * (lp.entry - lp.paper_entry) / lp.paper_entry * 100 if lp.paper_entry else None
        net = pnl.get("net")
        if reason == "exchange_closed" and net is not None:
            reason = "target" if net > 0 else "stop"         # only the stop and target orders can close it there
        self.state.record_trade({**asdict(lp), "closed_ms": now_ms, "reason": reason, **pnl,
                                 "r": (net / lp.risk_usdt) if net is not None and lp.risk_usdt else None,
                                 "entry_slippage_pct": slip})
        self.state.positions.pop(lp.symbol, None)
        self.state.save()

    async def monitor(self, now_ms: int) -> None:
        """Every minute: notice positions the exchange closed (stop/target hit), close expired ones, re-arm a missing
        stop, and obey the kill switch."""
        for sym, lp in list(self.state.positions.items()):
            try:
                pos = await self.client.position(sym)
            except BinanceError as e:
                self.state.note("error", symbol=sym, where="monitor", msg=str(e))
                continue
            if abs(pos["amt"]) == 0:
                try:
                    await self.client.cancel_all(sym)          # the other leg of stop/target is still resting
                except BinanceError:
                    pass
                await self._finish(lp, now_ms, "exchange_closed")
                continue
            if self.state.killed:
                await self.close(sym, now_ms, "kill_switch")
            elif now_ms >= lp.expires_ms:
                await self.close(sym, now_ms, "time_limit")
            else:
                try:
                    if await self.client.open_protection(sym) < 2:
                        close_side = "SELL" if lp.side == "long" else "BUY"
                        self.state.note("error", symbol=sym, where="monitor", msg="protection missing: re-armed")
                        await self.client.cancel_all(sym)
                        lp.protection = [await self.client.protect(sym, close_side, "STOP_MARKET", lp.stop),
                                         await self.client.protect(sym, close_side, "TAKE_PROFIT_MARKET", lp.target)]
                        self.state.save()
                except BinanceError as e:
                    self.state.note("error", symbol=sym, where="rearm", msg=str(e))
                    await self.close(sym, now_ms, "protection_failed")
