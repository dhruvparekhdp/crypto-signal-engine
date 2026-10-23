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

Limit entries (ExitModel.entry_limit_bps, owner 18 Oct 2026: scalping only pays with maker fees): the order rests at
the signal bar's close, improved by entry_limit_bps, for entry_window_min. It fills only when a later execution bar
trades THROUGH the price (a touch is not enough: the queue is ahead of us); otherwise there is no trade. A fill pays
the maker fee and no slippage. On the fill bar the stop can hit (worst case) but the target cannot.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from analysis.lab.costs import CostModel
from analysis.lab.data import INTERVAL_MS, Bars, load_funding
from analysis.lab import features as F

REASONS = ["stop", "target", "time", "breakeven", "trail", "partial_then_stop", "liquidity", "stale"]


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
    # 13 Oct 2026 "smart switch" options (owner): all lock profit for the NEXT bar, like be/trail
    ladder: tuple = ()                     # ((trigger_r, lock_r), ...): at trigger R profit, stop >= lock R
    giveback_after_r: float | None = None  # once this many R in profit...
    giveback_frac: float = 0.5             # ...exit if the trade gives back this share of its best profit
    stale_after_min: int | None = None     # after this long...
    stale_min_r: float = 1.0               # ...close at the bar close if it never reached this many R
    # Whipsaw guard (owner, 13 Oct): once the stop has been moved into profit (ladder / trail / giveback / be), it
    # fires only when a bar of this many minutes CLOSES beyond it, not on a wick. The original stop stays intrabar.
    lock_confirm_min: int | None = None
    lock_buffer_r: float = 0.0             # place every lock this many R further away (room for noise)
    # 18 Oct 2026 smart switch (owner: "fixed values are not smart"), on top of the ladder:
    flip_ema: int | None = None            # momentum flip: hourly close back across this-hour EMA AGAINST the trade...
    flip_gap_r: float = 0.5                # ...tightens the lock to best - gap (only once best >= flip_min_r)
    flip_min_r: float = 1.0
    decay_after_min: int | None = None     # no new best for this long (and best >= 1R) -> lock decay_frac of open profit
    decay_frac: float = 0.5
    # 18 Oct 2026 scalping research: a resting limit entry instead of a market entry at the next open
    entry_limit_bps: float | None = None   # limit at the signal close, this many bps better (None = market entry)
    entry_window_min: int = 15             # the limit is cancelled after this long unfilled
    # 23 Oct 2026 owner: "trade reached 2.8R then profit order trail to 3.25" - once the best price reaches trigger R
    # the target moves out to new_rr (never back in); pair it with a ladder step to lock the stop at the same time
    target_steps: tuple = ()               # ((trigger_r, new_rr), ...)

    def key(self) -> str:
        k = (f"sl{self.stop_atr}_rr{self.rr}_h{self.max_hold_min}_be{self.be_trigger_r}"
             f"_tr{self.trail_atr}_p{self.partial_r}")
        if self.trail_atr is not None and self.trail_after_r != 1.0:
            k += f"_ta{self.trail_after_r}"
        if self.ladder:
            k += "_lad" + "-".join(f"{a:g}:{b:g}" for a, b in self.ladder)
        if self.giveback_after_r is not None:
            k += f"_gb{self.giveback_after_r:g}x{self.giveback_frac:g}"
        if self.stale_after_min is not None:
            k += f"_st{self.stale_after_min}m{self.stale_min_r:g}"
        if self.lock_confirm_min is not None:
            k += f"_cf{self.lock_confirm_min}"
        if self.lock_buffer_r:
            k += f"_buf{self.lock_buffer_r:g}"
        if self.flip_ema:
            k += f"_flip{self.flip_ema}g{self.flip_gap_r:g}m{self.flip_min_r:g}"
        if self.decay_after_min:
            k += f"_decay{self.decay_after_min}x{self.decay_frac:g}"
        if self.entry_limit_bps is not None:
            k += f"_lim{self.entry_limit_bps:g}w{self.entry_window_min}"
        if self.target_steps:
            k += "_ts" + "-".join(f"{a:g}:{b:g}" for a, b in self.target_steps)
        return k


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
        limit_fill = False
        if exit.entry_limit_bps is not None:
            lim = float(sig_bars.c[si]) * (1 - side * exit.entry_limit_bps / 1e4)
            w_end = min(n - 2, e + max(1, exit.entry_window_min * 60_000 // INTERVAL_MS[ex.interval]) - 1)
            seg = ex.l[e:w_end + 1] if side > 0 else ex.h[e:w_end + 1]
            through = np.flatnonzero(seg < lim) if side > 0 else np.flatnonzero(seg > lim)
            if not len(through):
                continue                                 # never traded through the limit: no trade
            e = e + int(through[0])
            entry, limit_fill = lim, True
        else:
            raw = float(ex.o[e])
            entry = raw * (1 + slip) if side > 0 else raw * (1 - slip)
        stop_dist = exit.stop_atr * a
        stop_dist = max(stop_dist, exit.min_stop_pct * entry)
        if stop_dist > exit.max_stop_pct * entry:
            continue
        r_unit = stop_dist
        stop = entry - side * stop_dist
        init_stop = stop
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
        ema, last_best_t = None, int(ex.t[e])
        if exit.flip_ema:                            # seed the hourly EMA from the hours before entry
            kf = 2 / (exit.flip_ema + 1)
            for jj in range(max(0, e - exit.flip_ema * 3 * 60), e):
                if (int(ex.t[jj]) + INTERVAL_MS[ex.interval]) % 3_600_000 == 0:
                    cc = float(ex.c[jj])
                    ema = cc if ema is None else ema + (cc - ema) * kf
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
            locked = (side > 0 and stop > init_stop) or (side < 0 and stop < init_stop)
            if stop_hit and locked and exit.lock_confirm_min:
                stop_hit = False                     # a locked stop waits for a confirmed close (checked below)
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
            if tp_hit and limit_fill and j == e:
                tp_hit = False                           # filled at the bar's extreme: no target in the same bar
            if tp_hit:
                gap = (op >= tp) if side > 0 else (op <= tp)
                exit_px = op if gap else tp
                reason = "target"
                break
            if (exit.lock_confirm_min and ((side > 0 and stop > init_stop) or (side < 0 and stop < init_stop))
                    and ((int(ex.t[j]) + INTERVAL_MS[ex.interval]) % (exit.lock_confirm_min * 60_000) == 0)):
                c = float(ex.c[j])
                if (side > 0 and c <= stop) or (side < 0 and c >= stop):
                    exit_px = c * (1 - sslip) if side > 0 else c * (1 + sslip)
                    reason = "trail"
                    break
            # arm dynamic stops for the NEXT bar
            prev_best = best
            if side > 0:
                best = max(best, hi)
            else:
                best = min(best, lo)
            bar_end = int(ex.t[j]) + INTERVAL_MS[ex.interval]
            if best != prev_best:
                last_best_t = bar_end
            if be_px is not None and not armed_be and ((side > 0 and best >= be_px) or (side < 0 and best <= be_px)):
                armed_be = True
                if (side > 0 and be_stop > stop) or (side < 0 and be_stop < stop):
                    stop = be_stop
            if exit.trail_atr is not None and (armed_trail or (side > 0 and best >= trail_arm) or (side < 0 and best <= trail_arm)):
                armed_trail = True
                cand = best - side * exit.trail_atr * a
                if (side > 0 and cand > stop) or (side < 0 and cand < stop):
                    stop = cand
            best_r = side * (best - entry) / r_unit
            for trig, lock_r in exit.ladder:
                if best_r >= trig:
                    lk = lock_r - exit.lock_buffer_r
                    cand = (be_stop if lock_r <= 0 and not exit.lock_buffer_r else entry + side * lk * r_unit)
                    if (side > 0 and cand > stop) or (side < 0 and cand < stop):
                        stop = cand
                        armed_trail = True
            for trig, new_rr in exit.target_steps:
                if best_r >= trig:
                    cand = entry + side * new_rr * r_unit
                    if (side > 0 and cand > tp) or (side < 0 and cand < tp):
                        tp = cand
            if exit.flip_ema and bar_end % 3_600_000 == 0:
                cc = float(ex.c[j])
                ema = cc if ema is None else ema + (cc - ema) * (2 / (exit.flip_ema + 1))
                against = (side > 0 and cc < ema) or (side < 0 and cc > ema)
                if against and best_r >= exit.flip_min_r:
                    cand = entry + side * (best_r - exit.flip_gap_r) * r_unit
                    if (side > 0 and cand > stop) or (side < 0 and cand < stop):
                        stop = cand
                        armed_trail = True
            if (exit.decay_after_min and best_r >= 1.0
                    and bar_end - last_best_t >= exit.decay_after_min * 60_000):
                cand = entry + (best - entry) * exit.decay_frac
                if (side > 0 and cand > stop) or (side < 0 and cand < stop):
                    stop = cand
                    armed_trail = True
            if exit.giveback_after_r is not None and best_r >= exit.giveback_after_r:
                cand = entry + (best - entry) * (1 - exit.giveback_frac)
                if (side > 0 and cand > stop) or (side < 0 and cand < stop):
                    stop = cand
                    armed_trail = True
            if (exit.stale_after_min is not None and (j - e + 1) * INTERVAL_MS[ex.interval] >= exit.stale_after_min * 60_000
                    and best_r < exit.stale_min_r):
                c = float(ex.c[j])
                exit_px = c * (1 - slip) if side > 0 else c * (1 + slip)
                reason = "stale"
                break
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
        entry_fee = maker if limit_fill else taker
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
