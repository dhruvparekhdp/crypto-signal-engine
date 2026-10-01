"""Turn signals into trades, bar by bar, with conservative fills.

Rules (each is covered by a test):
  * A signal is read at bar close; entry is the NEXT execution bar's open, plus slippage.
  * Stop and target are placed from the signal bar's ATR, never from future data.
  * If one execution bar touches both stop and target, the stop is assumed to hit first.
  * A gap through the stop fills at the open, not at the stop.
  * A break-even or trailing stop only takes effect from the bar AFTER it is armed.
  * Funding is charged for every funding timestamp the trade is held across.
  * One open trade per symbol; a new signal while in a trade is ignored.
Costs are fractions of notional, so they do not depend on leverage; the wallet layer
turns a trade's net return into dollars.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from analysis.lab.costs import CostModel
from analysis.lab.data import INTERVAL_MS, Bars, load_funding
from analysis.lab import features as F

REASONS = ["stop", "target", "time", "breakeven", "trail", "partial_then_stop", "liquidity"]


@dataclass(frozen=True)
class ExitModel:
    stop_atr: float = 1.5
    rr: float = 2.0
    max_hold_min: int = 1440
    min_stop_pct: float = 0.003
    max_stop_pct: float = 0.08
    be_trigger_r: float | None = None      # arm break-even after this many R of profit
    trail_atr: float | None = None         # trail this many ATRs behind the best price once armed
    trail_after_r: float = 1.0
    partial_r: float | None = None         # take `partial_frac` off at this many R
    partial_frac: float = 0.5
    optimistic_ties: bool = False          # only for comparison; the default assumes the worst

    def key(self) -> str:
        return (f"sl{self.stop_atr}_rr{self.rr}_h{self.max_hold_min}_be{self.be_trigger_r}"
                f"_tr{self.trail_atr}_p{self.partial_r}")


COLS = ["symbol", "side", "entry_t", "exit_t", "entry", "exit", "stop_frac", "gross_ret",
        "fee_frac", "fund_frac", "net_ret", "r_net", "r_gross", "mae", "mfe", "bars", "reason", "sig_t"]


def simulate_symbol(sig_bars: Bars, signals: np.ndarray, ex: Bars, exit: ExitModel,
                    cost: CostModel, strategy: str = "") -> list[tuple]:
    """Trades for one symbol. `ex` is the execution series (1m for accuracy)."""
    idx = np.flatnonzero(signals)
    if not len(idx):
        return []
    atr = F.atr(sig_bars.h, sig_bars.l, sig_bars.c)
    close_t = sig_bars.close_t
    n = len(ex.t)
    start_idx = np.searchsorted(ex.t, close_t[idx], side="left")
    f_ts, f_rate = load_funding(ex.symbol)
    f_cum = np.r_[0.0, np.cumsum(f_rate)] if len(f_ts) else None
    slip = cost.slip_bps / 1e4
    sslip = cost.stop_slip_bps / 1e4
    taker, maker = cost.taker, cost.maker
    max_bars = max(1, exit.max_hold_min * 60_000 // INTERVAL_MS[ex.interval])
    rt_cost_price = cost.round_trip()
    out: list[tuple] = []
    last_exit = -1

    for k, si in enumerate(idx):
        e = int(start_idx[k])
        if e <= last_exit or e >= n - 2:
            continue
        side = int(signals[si])
        a = float(atr[si])
        if not a > 0:
            continue
        raw = float(ex.o[e])
        entry = raw * (1 + slip) if side > 0 else raw * (1 - slip)
        stop_dist = exit.stop_atr * a
        stop_dist = max(stop_dist, exit.min_stop_pct * entry)
        if stop_dist > exit.max_stop_pct * entry:
            continue
        r_unit = stop_dist
        stop = entry - side * stop_dist
        tp = entry + side * exit.rr * stop_dist
        part_px = entry + side * exit.partial_r * stop_dist if exit.partial_r else None
        be_px = entry + side * exit.be_trigger_r * stop_dist if exit.be_trigger_r else None
        trail_arm = entry + side * exit.trail_after_r * stop_dist
        be_stop = entry + side * rt_cost_price * entry
        best = entry
        armed_trail = False
        armed_be = False
        part_done = False
        part_gain = 0.0          # price gain taken at the partial, per unit
        mae = 0.0
        mfe = 0.0
        reason = "time"
        exit_px = None
        j_end = min(n - 1, e + max_bars)
        Hw, Lw, Ow = ex.h[e:j_end + 1].tolist(), ex.l[e:j_end + 1].tolist(), ex.o[e:j_end + 1].tolist()
        j = e
        while j <= j_end:
            hi, lo, op = Hw[j - e], Lw[j - e], Ow[j - e]
            if side > 0:
                adverse = (entry - lo) / entry
                fav = (hi - entry) / entry
                stop_hit = lo <= stop
                tp_hit = hi >= tp
                part_hit = (not part_done) and part_px is not None and hi >= part_px
            else:
                adverse = (hi - entry) / entry
                fav = (entry - lo) / entry
                stop_hit = hi >= stop
                tp_hit = lo <= tp
                part_hit = (not part_done) and part_px is not None and lo <= part_px
            if adverse > mae:
                mae = adverse
            if fav > mfe:
                mfe = fav
            if stop_hit and (not tp_hit or not exit.optimistic_ties):
                gap = (op <= stop) if side > 0 else (op >= stop)
                px = op if gap else stop
                exit_px = px * (1 - sslip) if side > 0 else px * (1 + sslip)
                if part_done:
                    reason = "partial_then_stop"
                elif armed_be and abs(stop - be_stop) < 1e-12:
                    reason = "breakeven"
                elif armed_trail and ((side > 0 and stop > entry) or (side < 0 and stop < entry)):
                    reason = "trail"
                else:
                    reason = "stop"
                break
            if part_hit and not part_done:
                part_done = True
                part_gain = side * (part_px - entry)
            if tp_hit:
                gap = (op >= tp) if side > 0 else (op <= tp)
                exit_px = op if gap else tp
                reason = "target"
                break
            # arm dynamic stops for the NEXT bar
            if side > 0:
                best = max(best, hi)
            else:
                best = min(best, lo)
            if be_px is not None and not armed_be and ((side > 0 and best >= be_px) or (side < 0 and best <= be_px)):
                armed_be = True
                if (side > 0 and be_stop > stop) or (side < 0 and be_stop < stop):
                    stop = be_stop
            if exit.trail_atr is not None and (armed_trail or (side > 0 and best >= trail_arm) or (side < 0 and best <= trail_arm)):
                armed_trail = True
                cand = best - side * exit.trail_atr * a
                if (side > 0 and cand > stop) or (side < 0 and cand < stop):
                    stop = cand
            j += 1
        if exit_px is None:
            jj = min(j, j_end)
            c = float(ex.c[jj])
            exit_px = c * (1 - slip) if side > 0 else c * (1 + slip)
            j = jj
            reason = "time"
        # --- accounting, per unit of entry price -------------------------------------
        frac = exit.partial_frac if part_done else 0.0
        rem_gain = side * (exit_px - entry)
        gross = (frac * part_gain + (1 - frac) * rem_gain) / entry
        entry_fee = taker
        exit_fee = (1 - frac) * (maker if (reason == "target" and cost.tp_maker) else taker) + frac * (maker if cost.tp_maker else taker)
        fee = entry_fee + exit_fee
        fund = 0.0
        if cost.funding and f_cum is not None:
            a0 = np.searchsorted(f_ts, ex.t[e], side="right")
            a1 = np.searchsorted(f_ts, ex.t[j] + INTERVAL_MS[ex.interval], side="right")
            fund = side * float(f_cum[a1] - f_cum[a0])
        net = gross - fee - fund
        sf = stop_dist / entry
        out.append((ex.symbol, side, int(ex.t[e]), int(ex.t[j]) + INTERVAL_MS[ex.interval], entry, exit_px, sf, gross,
                    fee, fund, net, net / sf, gross / sf, mae, mfe, j - e + 1, reason, int(close_t[si])))
        last_exit = j
    return out
