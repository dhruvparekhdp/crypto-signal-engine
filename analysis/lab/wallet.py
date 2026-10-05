"""The challenge wallet: start at 25 USDT, reset on 100 (target) or on bust.

Trades come in already simulated (net return as a fraction of notional, stop
distance, worst adverse move). This layer decides how much of the wallet each one
gets, whether it may open at all, and what a liquidation costs. A fair coin flip
reaches the target first with probability start/target = 25%, so a hit rate near
that is "no edge"; costs push it below.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class WalletConfig:
    start: float = 25.0
    target: float = 100.0
    bust_below: float = 1.5           # cannot fund the exchange minimum below this
    sizing: str = "risk_pct"          # risk_pct | margin_pct | fixed_notional
    risk_pct: float = 0.02            # of the balance, lost if the stop is hit
    margin_pct: float = 0.25
    fixed_notional: float = 50.0
    leverage: float = 5.0
    mmr: float = 0.005                # maintenance margin: liquidation at 1/lev - mmr
    max_concurrent: int = 1
    min_notional: float = 5.0         # Binance USD-M minimum order value
    max_margin_use: float = 0.9
    daily_loss_pct: float | None = None
    loss_streak_pause: int | None = None
    pause_hours: float = 2.0
    symbol_cooldown_min: int = 0
    reset: bool = True
    risk_mode: str = "fixed"           # fixed | adaptive: cut risk in drawdowns and after losing streaks
    dd_brake: tuple = ((0.10, 0.75), (0.20, 0.5))
    streak_brake: int = 3

    def label(self) -> str:
        if self.sizing == "risk_pct":
            return f"risk{self.risk_pct:.3f}{'a' if self.risk_mode == 'adaptive' else ''}_lev{self.leverage:g}_c{self.max_concurrent}"
        return f"{self.sizing}_lev{self.leverage:g}_c{self.max_concurrent}"


@dataclass
class WalletResult:
    cfg: WalletConfig
    cycles: list[dict] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)
    skipped: dict = field(default_factory=dict)
    discarded_on_reset: int = 0
    curve: list[tuple] = field(default_factory=list)

    def summary(self) -> dict:
        done = [c for c in self.cycles if c["status"] in ("TARGET", "BUST")]
        hit = sum(c["status"] == "TARGET" for c in done)
        bust = sum(c["status"] == "BUST" for c in done)
        base = self.cfg.start / self.cfg.target
        p = None
        if done:
            from scipy.stats import binomtest
            p = float(binomtest(hit, len(done), base, alternative="greater").pvalue)
        led = pd.DataFrame(self.ledger)
        return {
            "label": self.cfg.label(), "cycles_done": len(done), "targets": hit, "busts": bust,
            "in_progress": sum(c["status"] == "IN_PROGRESS" for c in self.cycles),
            "hit_rate": hit / len(done) if done else None, "fair_baseline": base,
            "p_vs_fair": p,
            "net_usdt": float(sum(c["end"] - c["start"] for c in self.cycles)),
            "trades": len(led), "liquidations": int((led.get("liquidated", pd.Series(dtype=bool))).sum()) if len(led) else 0,
            "avg_trades_per_cycle": float(np.mean([c["trades"] for c in done])) if done else None,
            "skipped": dict(self.skipped), "discarded_on_reset": self.discarded_on_reset,
        }


def run_wallet(trades: pd.DataFrame, cfg: WalletConfig) -> WalletResult:
    """trades needs: entry_t, exit_t (ms), symbol, net_ret, stop_frac, mae, and optionally strategy."""
    res = WalletResult(cfg)
    if trades is None or not len(trades):
        return res
    tr = trades.sort_values("entry_t", kind="stable").reset_index(drop=True)
    bal = cfg.start
    cycle = {"id": 1, "start": bal, "t0": int(tr.entry_t.iloc[0]), "trades": 0, "peak": bal, "trough": bal,
             "fees": 0.0}
    open_pos: list[dict] = []
    streak = 0
    pause_until = 0
    day_key, day_start = None, bal
    cool: dict[str, int] = {}

    def skip(why):
        res.skipped[why] = res.skipped.get(why, 0) + 1

    def close_cycle(status, t):
        nonlocal bal, cycle, streak, day_start
        cycle.update(status=status, end=bal, t1=t, dd=(cycle["peak"] - cycle["trough"]) / max(cycle["peak"], 1e-9))
        res.cycles.append(cycle)
        bal = cfg.start
        streak = 0
        day_start = bal
        cycle = {"id": cycle["id"] + 1, "start": bal, "t0": t, "trades": 0, "peak": bal, "trough": bal, "fees": 0.0}
        if open_pos:
            res.discarded_on_reset += len(open_pos)
            open_pos.clear()

    def settle(upto):
        nonlocal bal, streak
        for pos in sorted([p for p in open_pos if p["exit_t"] <= upto], key=lambda p: p["exit_t"]):
            if pos not in open_pos:
                continue
            open_pos.remove(pos)
            before = bal
            bal = max(0.0, bal + pos["pnl"])
            cycle["trades"] += 1
            cycle["peak"] = max(cycle["peak"], bal)
            cycle["trough"] = min(cycle["trough"], bal)
            cycle["fees"] += pos["fee_usd"]
            streak = streak + 1 if pos["pnl"] <= 0 else 0
            if pos["pnl"] <= 0 and cfg.symbol_cooldown_min:
                cool[pos["symbol"]] = pos["exit_t"] + cfg.symbol_cooldown_min * 60_000
            if cfg.loss_streak_pause and streak >= cfg.loss_streak_pause:
                nonlocal pause_until
                pause_until = max(pause_until, pos["exit_t"] + int(cfg.pause_hours * 3_600_000))
                streak = 0
            res.ledger.append({**pos["row"], "cycle": cycle["id"], "balance_before": before, "balance_after": bal,
                               "pnl": pos["pnl"], "t": pos["exit_t"]})
            res.curve.append((pos["exit_t"], bal))
            if bal >= cfg.target:
                close_cycle("TARGET", pos["exit_t"])
            elif bal < cfg.bust_below:
                close_cycle("BUST", pos["exit_t"])
                if not cfg.reset:
                    return True
        return False

    strat_col = "strategy" if "strategy" in tr.columns else None
    for row in tr.itertuples(index=False):
        if settle(row.entry_t):
            break
        t = int(row.entry_t)
        day = t // 86_400_000
        if day != day_key:
            day_key, day_start = day, bal
        if t < pause_until:
            skip("loss_streak_pause"); continue
        if cfg.daily_loss_pct and bal <= day_start * (1 - cfg.daily_loss_pct):
            skip("daily_loss_limit"); continue
        if cool.get(row.symbol, 0) > t:
            skip("symbol_cooldown"); continue
        if len(open_pos) >= cfg.max_concurrent:
            skip("max_concurrent"); continue
        if any(p["symbol"] == row.symbol for p in open_pos):
            skip("symbol_busy"); continue
        used = sum(p["margin"] for p in open_pos)
        free = bal - used
        if free < cfg.bust_below:
            skip("no_free_margin"); continue
        sf = float(row.stop_frac)
        risk = cfg.risk_pct
        if cfg.risk_mode == "adaptive":
            dd = 1 - bal / max(cycle["peak"], 1e-9)
            f_dd = min([m for l, m in cfg.dd_brake if dd >= l] or [1.0])
            risk = cfg.risk_pct * f_dd * (0.5 if streak >= cfg.streak_brake else 1.0)
        if cfg.sizing == "risk_pct":
            notional = bal * risk / sf
        elif cfg.sizing == "margin_pct":
            notional = bal * cfg.margin_pct * cfg.leverage
        else:
            notional = cfg.fixed_notional
        cap = free * cfg.max_margin_use * cfg.leverage
        capped = notional > cap
        notional = min(notional, cap)
        if notional < cfg.min_notional:
            skip("below_min_notional"); continue
        margin = notional / cfg.leverage
        liq_frac = 1.0 / cfg.leverage - cfg.mmr
        liquidated = bool(row.mae >= liq_frac and sf >= liq_frac * 0.98)
        pnl = -margin if liquidated else notional * float(row.net_ret)
        fee_usd = notional * (float(getattr(row, "fee_frac", 0.0)))
        open_pos.append({"symbol": row.symbol, "exit_t": int(row.exit_t), "margin": margin, "pnl": pnl,
                         "fee_usd": fee_usd,
                         "row": {"entry_t": t, "symbol": row.symbol, "side": int(row.side), "notional": notional,
                                 "margin": margin, "capped": capped, "liquidated": liquidated,
                                 "strategy": getattr(row, "strategy", "") if strat_col else "",
                                 "reason": getattr(row, "reason", "")}})
    settle(10**18)
    if cycle["trades"] or not res.cycles:
        cycle.update(status="IN_PROGRESS", end=bal, t1=int(tr.exit_t.iloc[-1]),
                     dd=(cycle["peak"] - cycle["trough"]) / max(cycle["peak"], 1e-9))
        res.cycles.append(cycle)
    return res


def cycle_odds(net_ret: np.ndarray, stop_frac: np.ndarray, cfg: WalletConfig, n_cycles: int = 20_000,
               max_trades: int = 600, seed: int = 11) -> dict:
    """Monte Carlo: bootstrap trades into one-position-at-a-time cycles. Ignores overlap and
    liquidation timing, so it answers 'does this edge and this sizing reach 100 before 0?'"""
    rng = np.random.default_rng(seed)
    net_ret = np.asarray(net_ret, float)
    stop_frac = np.asarray(stop_frac, float)
    bal = np.full(n_cycles, cfg.start)
    alive = np.ones(n_cycles, bool)
    result = np.zeros(n_cycles, np.int8)          # 1 target, -1 bust, 0 unfinished
    steps = np.zeros(n_cycles, np.int32)
    liq = 1.0 / cfg.leverage - cfg.mmr
    for s in range(max_trades):
        if not alive.any():
            break
        i = rng.integers(0, len(net_ret), n_cycles)
        nr, sf = net_ret[i], stop_frac[i]
        if cfg.sizing == "risk_pct":
            notional = bal * cfg.risk_pct / sf
        elif cfg.sizing == "margin_pct":
            notional = bal * cfg.margin_pct * cfg.leverage
        else:
            notional = np.full(n_cycles, cfg.fixed_notional)
        notional = np.minimum(notional, bal * cfg.max_margin_use * cfg.leverage)
        pnl = np.where(sf >= liq * 0.98, np.where(nr <= -liq, -notional / cfg.leverage, notional * nr), notional * nr)
        small = notional < cfg.min_notional
        bal = np.where(alive & ~small, np.maximum(0, bal + pnl), bal)
        steps += alive
        hit = alive & (bal >= cfg.target)
        bust = alive & ((bal < cfg.bust_below) | small)
        result[hit], result[bust] = 1, -1
        alive &= ~(hit | bust)
    done = result != 0
    return {"p_target": float((result == 1).sum() / max(done.sum(), 1)), "p_bust": float((result == -1).sum() / max(done.sum(), 1)),
            "unfinished": float((~done).mean()), "avg_trades": float(steps[done].mean()) if done.any() else None,
            "fair_baseline": cfg.start / cfg.target}
