"""
Leveraged futures position maths for the paper-trading simulator.

Pure functions and dataclasses only — no I/O, no global state — so the exact
same code runs in a historical backtest and in a live paper-trading cycle.
That equivalence is the point: a backtest result you cannot reproduce live is
worthless.

Conventions
-----------
* Fees are charged on NOTIONAL (margin x leverage), not on margin. This is the
  detail that makes leverage dangerous: at 10x, the 0.059% effective taker fee
  (0.05% + 18% GST) costs 0.59% of your margin per side, 1.18% round trip.
* Funding is charged every 8 hours a position stays open, so holding is not
  free even when price does not move.
* Liquidation is modelled exactly rather than with the usual "1/leverage"
  approximation, because notional shrinks as a long moves against you.
* Where a single candle contains both the stop and the target we assume the
  STOP filled first. We cannot know the path within a candle, and a simulator
  that resolves ambiguity in its own favour is worse than no simulator.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from analysis.scalp_levels import ScalpConfig
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class ExitReason(str, Enum):
    TARGET = "target"
    STOP = "stop"
    LIQUIDATION = "liquidation"
    EXPIRY = "expiry"
    CYCLE_END = "cycle_end"
    # Adaptive exits — the reason for holding stopped being true
    SIGNAL_FLIP = "signal_flip"          # the setup now points the other way
    CONVICTION_LOST = "conviction_lost"  # confidence decayed below the floor
    MARKET_SHOCK = "market_shock"        # violent move against an open position


@dataclass(frozen=True)
class FeeModel:
    """
    CoinDCX INR futures costs, calibrated against real account transactions
    rather than published rate cards.

    Reconciled 18 Aug 2026 against a live ETH/USDT position:
      * open fee Rs6.31 on a Rs10,681.44 position  -> 0.0591% effective
        which is 0.05% base x 1.18 GST. The 18% GST is charged on the
        brokerage and is NOT optional, so the effective rate is what matters.
      * liquidation at 1820.97 from entry 1906.50 at 20x (-4.49%) implies a
        maintenance margin near 0.53%, not the 1.5% quoted for larger tiers.
      * funding was Rs0.23 / Rs0.50 / Rs0.70 across three 8-hourly windows,
        roughly 0.0066% of notional per window.
    """

    taker_pct: float = 0.0005         # 0.05% base brokerage, market orders
    # Resting limit orders pay maker: 0.02% on both CoinDCX INR futures and
    # Binance USD-M (checked 26 Sep 2026). Only target exits are limit orders
    # here; entries, stops, trails and shocks are market orders and pay taker.
    maker_pct: float = 0.0002
    gst_pct: float = 0.18             # 18% GST on the brokerage, unavoidable
    maintenance_margin_pct: float = 0.0053   # measured, not the quoted 1.5%

    # Perpetual futures pay/charge funding every 8 hours while a position is
    # open. Ignoring it understates the cost of anything held for hours, which
    # is most of what this system does. Positive = longs pay shorts.
    funding_rate_per_8h: float = 0.0000655

    # Strictly, liquidating at the liquidation price leaves the maintenance margin
    # behind. In practice CoinDCX charges a liquidation clearance fee, and a fast
    # market fills you worse than the trigger price, so the residual usually
    # disappears. Default to the conservative assumption that a liquidation costs
    # the whole margin; set False to keep the exact residual.
    liquidation_consumes_margin: bool = True

    @property
    def effective_taker_pct(self) -> float:
        """What actually leaves the wallet, GST included."""
        return self.taker_pct * (1 + self.gst_pct)

    @property
    def effective_maker_pct(self) -> float:
        return min(self.maker_pct, self.taker_pct) * (1 + self.gst_pct)

    def entry_fee(self, notional: float) -> float:
        return notional * self.effective_taker_pct

    def exit_fee(self, notional: float, maker: bool = False) -> float:
        """A limit exit (the target) pays maker; everything else pays taker."""
        return notional * (self.effective_maker_pct if maker else self.effective_taker_pct)

    def funding_cost(self, notional: float, hours_held: float) -> float:
        """
        Funding paid over the life of a position, charged on notional.

        Modelled as a cost in both directions: the rate flips sign with market
        positioning, so assuming you always receive it would flatter results.
        """
        periods = max(0.0, hours_held) / 8.0
        return notional * self.funding_rate_per_8h * periods

    def round_trip_pct(self) -> float:
        """
        Price move required just to break even, as a fraction (leverage-independent).

        Taker both ways: the cost of a trade that ends on a stop or a trail,
        which is how most trades end. Deliberately the dearer figure for
        sizing and break-even; `target_round_trip_pct` is the cost of the
        trade that reaches its limit target.
        """
        return 2 * self.effective_taker_pct

    def target_round_trip_pct(self) -> float:
        """Taker in, maker out: the cost of a trade that fills its target."""
        return self.effective_taker_pct + self.effective_maker_pct


@dataclass(frozen=True)
class SlippageModel:
    """
    Where an order actually fills, versus the price the signal quoted.

    A signal that says "ETH at 1899" almost never fills at 1899. Three separate
    effects move it, and they do not all point the same way:

      1. The spread. A market buy lifts the ask, a market sell hits the bid.
         This is always adverse, whichever way you are trading.
      2. The trend. In a market drifting upward, a buy chases and fills higher
         while a sell gets lifted into and fills better. This is the one that
         makes 1899 become 1901 on a rally and 1898.2 on a slide, and it is
         signed by the drift, not by our direction — so it helps as often as it
         hurts.
      3. Stops specifically. A stop is a market order fired during the move
         that triggered it, so it gaps through the level. Always adverse, and
         larger than ordinary entry slippage.

    Targets are limit orders and fill at the limit or not at all, so they get
    no slippage. That is the honest treatment: giving targets a favourable fill
    would be the simulator flattering itself.

    Deterministic by construction — every term comes from the data, never from
    a random draw, so a backtest re-run gives the identical answer.
    """

    # Half-spread crossed on a market order. BTC/ETH perps sit near 1bp.
    spread_pct: float = 0.0001

    # How much of the recent per-bar drift is carried into the fill. 0.30 means
    # a bar that moved 1% pushes the fill 0.30% in that direction.
    trend_impact: float = 0.30

    # Extra adverse move a stop suffers over and above the spread.
    stop_extra_pct: float = 0.0005

    # A liquidation is the exchange closing you at market during a violent
    # move; it is the worst fill on the book.
    liquidation_extra_pct: float = 0.0015

    # Nothing is allowed to move a fill further than this, so one freak bar
    # cannot dominate a whole backtest.
    max_slip_pct: float = 0.0060

    def _clamp(self, slip: float) -> float:
        return max(-self.max_slip_pct, min(self.max_slip_pct, slip))

    def fill(self, ref_price: float, buying: bool, drift_pct: float = 0.0,
             extra_adverse_pct: float = 0.0) -> float:
        """
        Fill price for a market order.

        `buying` is True when we are lifting offers — opening a LONG or closing
        a SHORT. `drift_pct` is the recent per-bar price change as a fraction;
        positive means the market is rising.
        """
        adverse = self.spread_pct + extra_adverse_pct
        directional = self.trend_impact * drift_pct
        # Adverse always pushes against us; directional follows the market.
        slip = (adverse if buying else -adverse) + directional
        return ref_price * (1.0 + self._clamp(slip))

    def entry_fill(self, ref_price: float, side: Side, drift_pct: float = 0.0) -> float:
        return self.fill(ref_price, buying=side is Side.LONG, drift_pct=drift_pct)

    def exit_fill(self, ref_price: float, side: Side, reason: ExitReason,
                  drift_pct: float = 0.0) -> float:
        """Closing a LONG means selling; closing a SHORT means buying."""
        if reason is ExitReason.TARGET:
            # The only limit order in the set. Everything else is a market
            # order and pays the spread; a stop or a shock pays more.
            return ref_price
        buying = side is Side.SHORT
        if reason is ExitReason.MARKET_SHOCK:
            # Fired during a violent bar, so it fills like a stop, not like a
            # calm market order. Anything less understates the cost of panic.
            extra = self.stop_extra_pct
            drift_pct = abs(drift_pct) * (1.0 if buying else -1.0)
            return self.fill(ref_price, buying=buying, drift_pct=drift_pct,
                             extra_adverse_pct=extra)
        if reason in (ExitReason.STOP, ExitReason.LIQUIDATION):
            extra = (self.stop_extra_pct if reason is ExitReason.STOP
                     else self.liquidation_extra_pct)
            # A stop is triggered BY an adverse move, so the local direction is
            # against us no matter what the preceding bars did. Feeding the raw
            # drift in here would let a prior uptrend fill a long's stop above
            # the stop level, which cannot happen — and would quietly flatter
            # every losing trade in the backtest. Volatility makes the gap
            # bigger, never favourable, so only its magnitude is used.
            drift_pct = abs(drift_pct) * (1.0 if buying else -1.0)
        else:
            extra = 0.0
        return self.fill(ref_price, buying=buying,
                         drift_pct=drift_pct, extra_adverse_pct=extra)


NO_SLIPPAGE = SlippageModel(spread_pct=0.0, trend_impact=0.0,
                            stop_extra_pct=0.0, liquidation_extra_pct=0.0)


def sign_of(side: Side) -> float:
    """+1 for a long, -1 for a short.

    Every price relation in this module is the same formula with one sign
    flipped. Writing it once with a multiplier keeps the two halves from
    drifting apart, which is the usual way a short-side bug survives review.
    """
    return 1.0 if side is Side.LONG else -1.0


def round_to_lot(qty: float, lot_step: float) -> float:
    """
    Snap a raw quantity to the instrument's lot step, rounding to nearest.

    CoinDCX rounds to nearest rather than truncating: a Rs533 ETH position at
    20x with entry 1906.50 works out to 0.054888 raw, and the account shows
    0.055, which only happens if you round up at the halfway point. A lot_step
    of 0 means "no rounding" and returns the raw quantity untouched.
    """
    if lot_step <= 0:
        return qty
    steps = qty / lot_step
    # round-half-up, because Python's round() is banker's rounding and would
    # send an exact .5 lot to the even neighbour instead of always upward.
    return math.floor(steps + 0.5) * lot_step


def liquidation_price(entry: float, side: Side, leverage: float, mm_pct: float) -> float:
    """
    Exact liquidation price — where equity falls to the maintenance requirement.

    LONG:  entry * (1 - 1/L) / (1 - mm)
    SHORT: entry * (1 + 1/L) / (1 + mm)

    Derived from  margin + (P - entry)*qty == P*qty*mm  (and its short mirror),
    so it accounts for the position's own notional changing with price. The
    common `entry * (1 - 1/L)` shortcut is slightly pessimistic for longs.
    """
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    inv = 1.0 / leverage
    s = sign_of(side)
    return entry * (1.0 - s * inv) / (1.0 - s * mm_pct)


def stop_and_target(
    entry: float,
    side: Side,
    leverage: float,
    stop_pct_of_margin: float,
    reward_risk: float,
) -> tuple[float, float]:
    """
    Convert a risk budget expressed in margin terms into actual prices.

    stop_pct_of_margin=0.20 at 10x means "risk 20% of margin", which is a
    20%/10 = 2.0% adverse price move. The target is placed reward_risk times
    that distance away, which is the ratio that decides whether the strategy
    can survive fees at all.
    """
    stop_move = stop_pct_of_margin / leverage
    target_move = stop_move * reward_risk
    s = sign_of(side)
    return entry * (1.0 - s * stop_move), entry * (1.0 + s * target_move)


@dataclass
class Position:
    symbol: str
    side: Side
    entry_price: float
    margin: float
    leverage: float
    stop_price: float
    target_price: float
    liq_price: float
    opened_at: datetime
    entry_fee: float
    signal_type: str = ""
    timeframe: str = ""
    confidence: float = 0.0
    expires_at: datetime | None = None

    # Quote conversion. Defaults keep prices and margin in the same currency
    # with no lot rounding, which is what the backtest harness assumes.
    usdt_inr: float = 1.0
    lot_step: float = 0.0

    # What the signal quoted, before slippage. entry_price is where we filled.
    signal_price: float = 0.0

    # Trailing state. initial_stop_price is kept because the stop itself moves,
    # and 1R has to stay measured from the risk we originally accepted.
    initial_stop_price: float = 0.0
    peak_price: float = 0.0        # best price seen in our favour

    # Set each tick by the position reviewer, in R. None means "use whatever
    # the trailing config says" — the state a position is in before any
    # review has run, and the state it stays in if reviews are switched off.
    trail_r_override: float | None = None
    trail_active: bool = False
    # Highest rung the ladder has locked, so it can never step back down.
    locked_roe: float | None = None

    # Set once the position has been scaled in or out, because after that the
    # size no longer follows from margin x leverage / entry.
    _coin_qty: float | None = None

    @property
    def target_notional(self) -> float:
        """What we asked for, before the exchange rounded the quantity to a lot."""
        return self.margin * self.leverage

    @property
    def coin_qty(self) -> float:
        """
        Position size in coins, as the exchange actually fills it.

        This is the number the account screen shows (0.055 ETH), and it is not
        target_notional / entry_price: the price is quoted in USDT while the
        margin is in INR, so the rate has to divide out first. Then the result
        is snapped to the instrument's lot step, which is why the filled
        notional rarely equals margin x leverage exactly.
        """
        if self._coin_qty is not None:
            return self._coin_qty
        raw = self.target_notional / (self.usdt_inr * self.entry_price)
        return round_to_lot(raw, self.lot_step)

    @property
    def notional(self) -> float:
        """Filled notional in INR — coins x price x rate, after lot rounding."""
        return self.coin_qty * self.entry_price * self.usdt_inr

    @property
    def quantity(self) -> float:
        """
        P&L multiplier: INR earned per 1 USDT of price move.

        Kept as a separate property from coin_qty so that gross_pnl and the
        fee helpers can stay in INR while prices stay in USDT.
        """
        return self.coin_qty * self.usdt_inr

    @property
    def sign(self) -> float:
        return sign_of(self.side)

    def favourable_move(self, price: float) -> float:
        """Price distance in our favour. Negative when the trade is offside."""
        return self.sign * (price - self.entry_price)

    def gross_pnl(self, exit_price: float) -> float:
        return self.favourable_move(exit_price) * self.quantity

    def mark_notional(self, mark: float) -> float:
        """
        Notional at the current mark, which is the "position size" the CoinDCX
        screen shows — not the entry notional. On the reconciled ETH trade the
        two differ by Rs18, which is exactly the open-to-LTP drift.
        """
        return self.coin_qty * mark * self.usdt_inr

    @property
    def risk_per_unit(self) -> float:
        """Initial stop distance in price terms. This is 1R."""
        base = self.initial_stop_price or self.stop_price
        return abs(self.entry_price - base)

    def r_multiple(self, price: float) -> float:
        """How many R the trade is up at `price`. Negative means offside."""
        risk = self.risk_per_unit
        return self.favourable_move(price) / risk if risk > 0 else 0.0

    def peak_roe(self, mark: float) -> float:
        """Best return-on-margin this position has been worth, as a fraction."""
        best = self.peak_price or mark
        best = max(best, mark) if self.side is Side.LONG else min(best, mark)
        if self.margin <= 0:
            return 0.0
        return self.favourable_move(best) * self.quantity / self.margin

    def apply_ladder(self, mark: float, ladder: ProfitLadder,
                     fees: FeeModel) -> bool:
        """
        Move the stop up a rung if the trade has earned it. Returns True if it moved.

        Ratchets only: `locked_roe` never decreases, so a pullback after a rung
        is reached cannot give the lock back.
        """
        if not ladder.enabled or self.margin <= 0 or self.leverage <= 0:
            return False

        s = self.sign
        seen = self.peak_price or mark
        extreme = max(seen, mark) if s > 0 else min(seen, mark)
        self.peak_price = extreme

        target = ladder.locked_roe(self.peak_roe(mark))
        if target is None:
            return False
        if self.locked_roe is not None and target <= self.locked_roe:
            return False

        # ROE back to price: a locked ROE of x needs x/leverage of price move.
        move = target / self.leverage
        if ladder.cover_costs_at_breakeven:
            move += fees.round_trip_pct()
        candidate = self.entry_price * (1 + s * move)

        if s * (candidate - self.stop_price) <= 0:
            return False
        self.stop_price = candidate
        self.locked_roe = target
        return True

    def apply_profit_lock(self, price: float, lock: ProfitLock, fees: FeeModel,
                          slippage=None) -> bool:
        """
        Tighten the stop by the profit-lock rule. Returns True if it moved.

        Runs after the tick's exits were checked, so it acts from the next
        tick. The stop only ever tightens, and never:
          * locks less than the round trip in fees (with GST) plus the spread
            and stop slippage: a "locked" win must still be a win after costs
          * sits closer to the price than the trail distance: a lock must not
            stop the trade out on the tick it arms
        """
        if not lock.enabled or price <= 0 or self.entry_price <= 0:
            return False
        s = self.sign
        self.peak_price = s * max(s * (self.peak_price or price), s * price)
        best = self.peak_price
        if s * (best - self.entry_price) / self.entry_price * 100 < lock.at_pct:
            return False
        cost = fees.round_trip_pct()
        if slippage is not None:
            cost += 2 * slippage.spread_pct + slippage.stop_extra_pct
        lock_to = max(lock.to_pct / 100, cost)
        new = self.entry_price * (1 + s * lock_to)
        if lock.trail_pct:
            trail = best * (1 - s * lock.trail_pct / 100)
            new = max(new, trail) if s > 0 else min(new, trail)
        ceiling = price * (1 - s * (lock.trail_pct or 0.05) / 100)
        new = min(new, ceiling) if s > 0 else max(new, ceiling)
        if s * (new - self.stop_price) <= 0:
            return False
        self.stop_price = new
        self.trail_active = True
        return True

    def update_trail(self, high: float, low: float, trail: TrailingStop,
                     fees: FeeModel) -> bool:
        """
        Ratchet the stop after this bar has already been checked against the
        existing one. Returns True if the stop moved.

        Call order matters and is not a detail: updating the trail before
        resolving the bar would let this bar's high drag the stop above this
        bar's low, and a trade that actually stopped out would survive.
        """
        if not trail.enabled or self.leverage <= 0:
            return False

        s = self.sign
        extreme = high if self.side is Side.LONG else low
        # s * best is always the maximum, whichever side we are on.
        best = s * max(s * (self.peak_price or extreme), s * extreme)
        self.peak_price = best

        risk = self.risk_per_unit
        if risk <= 0:
            return False
        if not self.trail_active:
            if self.favourable_move(best) < trail.activate_at_r * risk:
                return False
            self.trail_active = True
            if trail.release_target:
                # Nothing else closes the trade now, so the trail has to.
                self.target_price = s * math.inf

        # In R when the setup says so, otherwise % of margin converted to price
        # by dividing out the leverage.
        # An override set per tick by the position reviewer, from how much
        # conviction the local read still has. More confidence rides looser;
        # less takes what is on the table. It wins over the configured value
        # because it is the more recent judgement about this specific trade.
        if self.trail_r_override is not None:
            trail_move = risk * self.trail_r_override
        elif trail.trail_r is not None:
            trail_move = risk * trail.trail_r
        else:
            trail_move = self.entry_price * trail.trail_pct_of_margin / self.leverage

        # A trail can never ride further from price than the stop it replaces.
        #
        # The margin-denominated form above divides by leverage, so it was
        # calibrated for a world where leverage was fixed at 10x. Now that
        # leverage falls as the stop widens — to hold the loss per trade at the
        # risk budget — a 2x position produced a trail five times wider than
        # its own stop. The ratchet only ever moves the stop toward price, so
        # that trail could not move it at all and trailing silently stopped
        # working. Clamping is the honest reading: a stop further away than the
        # one already set is not a trailing stop, it is a looser one.
        trail_move = min(trail_move, risk)
        if trail.step_r is not None:
            step = risk * trail.step_r
        else:
            step = self.entry_price * trail.step_pct_of_margin / self.leverage
        # The step exists to stop the level twitching on every bar, so it has
        # to stay small against the move it is gating. The same leverage
        # division that widened the trail also inflated this: at 2x it came out
        # larger than the whole trail distance, so no improvement was ever big
        # enough to be written and the trail activated but never moved. A
        # quarter of the trail is a threshold; more than the trail is a veto.
        step = min(step, trail_move * 0.25)
        candidate = best - s * trail_move

        if trail.lock_breakeven:
            # Entry plus the round trip, so the floor is a scratch not a loss.
            be = self.entry_price * (
                1 + s * (fees.round_trip_pct() + trail.breakeven_buffer_pct))
            candidate = s * max(s * candidate, s * be)

        moved = s * (candidate - self.stop_price) > step
        if moved:
            self.stop_price = candidate
        return moved

    @property
    def entry_slippage_pct(self) -> float:
        """How far the fill landed from the quoted price, signed against us."""
        if not self.signal_price:
            return 0.0
        raw = (self.entry_price - self.signal_price) / self.signal_price
        return raw if self.side is Side.LONG else -raw

    @property
    def effective_leverage(self) -> float:
        """Filled notional over margin. Drifts from the nominal leverage once
        lot rounding or a scale-in has moved the size."""
        return self.notional / self.margin if self.margin > 0 else 0.0

    def _reprice_levels(self, stop_pct_of_margin: float, reward_risk: float,
                        fees: FeeModel) -> None:
        lev = self.effective_leverage
        self.stop_price, self.target_price = stop_and_target(
            self.entry_price, self.side, lev, stop_pct_of_margin, reward_risk
        )
        self.liq_price = liquidation_price(
            self.entry_price, self.side, lev, fees.maintenance_margin_pct
        )

    def increase(self, add_margin: float, price: float, fees: FeeModel,
                 stop_pct_of_margin: float, reward_risk: float) -> float:
        """
        Add margin to an open position (scale in). Returns the fee charged.

        The average entry moves toward the new fill, so the stop, target and
        liquidation all have to be recomputed off it — leaving them anchored to
        the original entry is the classic way a scaled-in position ends up with
        a stop that is already behind price.
        """
        if add_margin <= 0:
            return 0.0
        add_qty = round_to_lot(add_margin * self.leverage / (self.usdt_inr * price),
                               self.lot_step)
        if add_qty <= 0:
            return 0.0  # too small to buy even one lot

        old_qty = self.coin_qty
        new_qty = old_qty + add_qty
        self.entry_price = (old_qty * self.entry_price + add_qty * price) / new_qty
        self._coin_qty = new_qty
        self.margin += add_margin

        fee = fees.entry_fee(add_qty * price * self.usdt_inr)
        self.entry_fee += fee
        self._reprice_levels(stop_pct_of_margin, reward_risk, fees)
        return fee

    def reduce(self, close_qty: float, price: float, fees: FeeModel,
               fee_model_exit: bool = True) -> tuple[float, float, float]:
        """
        Close part of a position (scale out).

        Returns (gross P&L realised, exit fee paid, margin freed). The entry
        price is deliberately left alone: taking profit off the table does not
        change what the remaining coins cost. Margin is released pro rata, so
        the effective leverage — and therefore the liquidation price — is
        unchanged, which is the property the tests below pin.
        """
        held = self.coin_qty
        close_qty = round_to_lot(min(max(close_qty, 0.0), held), self.lot_step)
        if close_qty <= 0:
            return 0.0, 0.0, 0.0

        gross = self.favourable_move(price) * close_qty * self.usdt_inr
        fee = fees.exit_fee(close_qty * price * self.usdt_inr) if fee_model_exit else 0.0

        share = close_qty / held
        freed = self.margin * share
        self.margin -= freed
        self.entry_fee -= self.entry_fee * share  # the closed part's open fee is now spent
        self._coin_qty = held - close_qty
        return gross, fee, freed

    def unrealised(self, mark: float, fees: FeeModel) -> float:
        """Net P&L if closed right now, including the exit fee not yet paid."""
        return self.gross_pnl(mark) - fees.exit_fee(mark * self.quantity)


@dataclass
class ClosedTrade:
    position: Position
    exit_price: float
    closed_at: datetime
    reason: ExitReason
    gross_pnl: float
    fees_paid: float          # trading fees + funding, all-in
    net_pnl: float
    wallet_after: float
    funding_paid: float = 0.0
    hours_held: float = 0.0

    @property
    def entry_slippage_pct(self) -> float:
        """Entry fill versus the quoted signal price, signed against us."""
        return self.position.entry_slippage_pct

    @property
    def return_on_margin(self) -> float:
        return self.net_pnl / self.position.margin if self.position.margin else 0.0

    @property
    def won(self) -> bool:
        return self.net_pnl > 0


def open_position(
    symbol: str,
    side: Side,
    entry_price: float,
    margin: float,
    leverage: float,
    fees: FeeModel,
    stop_pct_of_margin: float,
    reward_risk: float,
    opened_at: datetime,
    signal_type: str = "",
    timeframe: str = "",
    confidence: float = 0.0,
    expires_at: datetime | None = None,
    usdt_inr: float = 1.0,
    lot_step: float = 0.0,
    slippage: SlippageModel = NO_SLIPPAGE,
    drift_pct: float = 0.0,
    stop_price: float | None = None,
    target_price: float | None = None,
) -> Position:
    # The signal quotes a price; we fill somewhere near it. Everything after
    # this point — stop, target, liquidation, size — is measured from where we
    # actually got in, because that is the position we are actually holding.
    signal_price = entry_price
    entry_price = slippage.entry_fill(entry_price, side, drift_pct)

    # Levels the caller supplies win. Deriving them from a margin-risk budget
    # instead produced a book that shared only direction and timing with the
    # signals it claimed to be testing: a gold signal published a 0.231%
    # target and the trade opened against a 4% one, seventeen times wider, so
    # it expired untouched and paid fees. A paper trade that does not take the
    # signal's own levels is measuring a different strategy.
    if stop_price is not None and target_price is not None:
        stop, target = stop_price, target_price
    else:
        stop, target = stop_and_target(
            entry_price, side, leverage, stop_pct_of_margin, reward_risk)
    pos = Position(
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        margin=margin,
        leverage=leverage,
        stop_price=stop,
        target_price=target,
        liq_price=liquidation_price(entry_price, side, leverage, fees.maintenance_margin_pct),
        opened_at=opened_at,
        entry_fee=0.0,
        signal_type=signal_type,
        timeframe=timeframe,
        confidence=confidence,
        expires_at=expires_at,
        usdt_inr=usdt_inr,
        lot_step=lot_step,
        signal_price=signal_price,
        initial_stop_price=stop,
    )
    # Charged on what actually filled, not on what we asked for. With a coarse
    # lot step those differ by enough to matter on a small wallet.
    pos.entry_fee = fees.entry_fee(pos.notional)
    return pos


def resolve_candle(
    pos: Position,
    high: float,
    low: float,
    close: float,
    ts: datetime,
    slippage: SlippageModel = NO_SLIPPAGE,
    drift_pct: float = 0.0,
) -> tuple[ExitReason, float] | None:
    """
    Decide whether a candle closes this position, and at what price.

    Priority is deliberate and pessimistic:
      1. Liquidation — a hard exchange action that overrides any of our orders.
      2. Stop — if both stop and target are inside the candle's range we cannot
         know which was touched first, so we book the loss.
      3. Target.
      4. Time expiry, filled at the close.

    Returns None if the position survives the candle.
    """
    # Stop versus liquidation is decided by which level price REACHES first,
    # not by a fixed precedence. A long falling toward both passes the higher
    # level first, so a stop above the liquidation price fires before it — and
    # once the trail has ratcheted the stop up, that is the normal case.
    # Booking a liquidation there would invent losses that cannot happen.
    # Stop versus liquidation is decided by which level price REACHES first,
    # not by a fixed precedence. A long falling toward both passes the higher
    # level first, so a stop above the liquidation price fires before it — and
    # once the trail has ratcheted the stop up, that is the normal case.
    # Booking a liquidation there would invent losses that cannot happen.
    s = pos.sign
    adverse = low if pos.side is Side.LONG else high    # the way a loss lies
    favour = high if pos.side is Side.LONG else low     # the way a win lies

    hit: tuple[ExitReason, float] | None = None
    if s * pos.stop_price > s * pos.liq_price:
        first, reason = pos.stop_price, ExitReason.STOP
    else:
        first, reason = pos.liq_price, ExitReason.LIQUIDATION

    if s * adverse <= s * first:
        hit = (reason, first)
    elif s * favour >= s * pos.target_price:
        hit = (ExitReason.TARGET, pos.target_price)

    if hit is None and pos.expires_at is not None and ts >= pos.expires_at:
        hit = (ExitReason.EXPIRY, close)
    if hit is None:
        return None

    reason, level = hit
    return reason, slippage.exit_fill(level, pos.side, reason, drift_pct)


def close_position(
    pos: Position,
    exit_price: float,
    reason: ExitReason,
    closed_at: datetime,
    fees: FeeModel,
    wallet_before: float,
) -> ClosedTrade:
    """
    Settle a position and return the wallet impact.

    The margin was already deducted from the wallet when the position opened,
    so settlement returns margin + net P&L. A liquidation is floored at losing
    the entire margin — you cannot lose more than you posted.
    """
    gross = pos.gross_pnl(exit_price)
    exit_fee = fees.exit_fee(exit_price * pos.quantity, maker=reason is ExitReason.TARGET)
    hours_held = max(0.0, (closed_at - pos.opened_at).total_seconds() / 3600.0)
    funding = fees.funding_cost(pos.notional, hours_held)
    total_fees = pos.entry_fee + exit_fee + funding
    net = gross - total_fees

    if reason is ExitReason.LIQUIDATION and fees.liquidation_consumes_margin:
        # Clearance fee + slippage past the trigger price: assume nothing comes back.
        net = -pos.margin

    if net < -pos.margin:            # you can never lose more than you posted
        net = -pos.margin

    return ClosedTrade(
        position=pos,
        exit_price=exit_price,
        closed_at=closed_at,
        reason=reason,
        gross_pnl=gross,
        fees_paid=total_fees,
        funding_paid=funding,
        hours_held=hours_held,
        net_pnl=net,
        wallet_after=wallet_before + pos.margin + net,
    )


@dataclass(frozen=True)
class ProfitLadder:
    """
    Ratchet the stop as return-on-margin crosses rungs, locking a share of the
    gain at each one.

    This is the pattern from the account, made mechanical. A live SUI long at
    25x sat at +11.47% ROE with its stop moved to +7.53% ROE — above entry, so
    the worst case had become a profit of Rs86 rather than a loss. Earlier the
    same thing was done by hand on another trade, the stop moved five times as
    it ran.

    Each rung is (roe_reached, roe_locked): once the trade has been worth
    roe_reached, the stop moves to wherever it would return roe_locked. Rungs
    ascend, so the stop can only climb — the ratchet is the point.

    Two deliberate properties:

    * The first rung locks BREAK-EVEN, not a profit. Locking a gain before
      there is room to give one back just converts winners into scratches.
    * The lock always trails the trigger. A rung that locked what it triggered
      on would stop the trade out at the exact price that armed it.
    """

    enabled: bool = False
    rungs: tuple[tuple[float, float], ...] = (
        (0.20, 0.00),   # +20% ROE reached -> stop to break-even
        (0.35, 0.12),   # +35%            -> keep at least +12%
        (0.60, 0.30),
        (1.00, 0.60),
        (1.75, 1.20),
        (3.00, 2.20),
    )

    # Break-even means entry plus the round trip, not entry. Stopping out at
    # entry still loses both fees.
    cover_costs_at_breakeven: bool = True

    @classmethod
    def tight(cls) -> ProfitLadder:
        """
        Locks earlier and keeps more, closer to how the account actually trades.

        The live SUI position had its stop at +7.53% ROE while the trade was
        worth +11.47% — 66% of the open gain protected almost immediately.
        That converts more winners into small wins and fewer into large ones;
        the default leaves more room before the first rung.
        """
        return cls(enabled=True, rungs=(
            (0.10, 0.00),
            (0.15, 0.07),
            (0.30, 0.18),
            (0.60, 0.40),
            (1.00, 0.75),
            (2.00, 1.60),
            (3.00, 2.50),
        ))

    def locked_roe(self, peak_roe: float) -> float | None:
        """Highest rung the trade has earned, or None if it has earned none."""
        best = None
        for reached, locked in self.rungs:
            if peak_roe >= reached:
                best = locked if best is None else max(best, locked)
        return best


@dataclass(frozen=True)
class ProfitLock:
    """
    The owner's exit (26 Sep SOL long: entry 120.69, stop pulled to 121.15 once
    price reached ~121.3). Once price has moved `at_pct` in our favour, the
    stop jumps to `to_pct` beyond entry, then trails `trail_pct` behind the
    best price. Small wins kept, few big losses. Percent of price, not R.
    """

    enabled: bool = True
    at_pct: float = 0.5
    to_pct: float = 0.35
    trail_pct: float = 0.15


@dataclass(frozen=True)
class TrailingStop:
    """
    Ratchet the stop forward while a trade is working.

    The fixed 20% stop answers "how much am I willing to lose". It says nothing
    about giving back a gain that already happened. A trade that runs +18% and
    then reverses closes for a full -20% loss under a fixed stop, even though
    it was never wrong.

    Distances are expressed as a percentage OF MARGIN, matching how the stop is
    set, and converted to a price move by dividing by leverage. At 10x, 20% of
    margin is a 2.0% price move.

    Two invariants the tests pin, because breaking either turns this from a
    risk control into a way of losing money slowly:
      * The stop only ever moves in our favour. Never widened, ever.
      * The trail is updated from a bar only AFTER that bar has been checked
        against the existing stop. Otherwise this bar's high could drag the
        stop up past this bar's low, and a losing trade would quietly survive.
    """

    enabled: bool = False

    # How far the trade must be in profit before trailing starts, in R — where
    # 1R is the initial stop distance. Trailing from the first tick strangles
    # trades in ordinary noise before they have room to work.
    activate_at_r: float = 1.0

    # How far behind the best price the stop rides, as a % of margin.
    trail_pct_of_margin: float = 0.20

    # Minimum move before the stop is rewritten. Stops the level twitching on
    # every bar, which in a live account is a stream of order amendments.
    step_pct_of_margin: float = 0.05

    # The same two distances expressed in R — multiples of the initial stop —
    # and preferred whenever they are set.
    #
    # The margin-denominated versions were written for the fixed 20/20 world,
    # where the stop IS 20% of margin and a 20% trail is exactly 1R. Signal
    # levels are ATR-derived and much tighter: an ETH stop of 0.426% against a
    # trail of 0.20/10x = 2.0% of price puts the trail nearly five times
    # further from the high than the original stop is from entry, so it can
    # never ratchet anything and the feature silently does nothing. In R the
    # distance travels with the setup instead of with the leverage dial.
    trail_r: float | None = None
    step_r: float | None = None

    # On activation, jump the stop to entry plus the round-trip fee, so the
    # worst case becomes a scratch rather than a small loss.
    lock_breakeven: bool = True

    # What "breakeven" has to clear. FeeModel knows brokerage; the spread
    # crossed twice and the slippage beyond it are separate models, so their
    # cost is added here. Without it the trail locks a stop 0.05% short of
    # flat and calls a small loss a scratch.
    breakeven_buffer_pct: float = 0.0005

    # Let a winner run past the fixed target instead of taking it. Only sane
    # WITH a trail, since otherwise nothing closes the trade.
    release_target: bool = False

    @classmethod
    def runner(cls) -> TrailingStop:
        """
        Let winners run, cut losers at 1R. The point of pairing a trail with a
        reward:risk above 1.

        Arms at 1.25R — before the 2R target, or the position would close at
        the target first and the trail would be dead code — and the fixed
        target is released so nothing caps the upside but the trail itself.
        """
        # Arms at 1.25R with no breakeven jump. At 0.75R with a jump to
        # breakeven, 9 of 42 paper trades (21-25 Sep) came good by +0.1-0.3%,
        # armed, and were closed by ordinary noise at exactly entry plus fees.
        # Armed at 1.25R and riding up to 1R behind, the worst case after
        # arming is still a small profit, and a normal wiggle no longer ends
        # the trade.
        return cls(enabled=True, activate_at_r=1.25, trail_r=1.0, step_r=0.10,
                   lock_breakeven=False, release_target=True)


@dataclass(frozen=True)
class LeverageConfig:
    """
    Leverage scaled by confidence, bounded by what the market can gap through.

    Leverage does NOT change the break-even move, and it does not change where
    the stop sits relative to liquidation — at a 20% margin stop the stop is
    20% of the way to liquidation at every leverage. What it changes is the
    ABSOLUTE distance, and therefore whether an ordinary session covers it:

        10x  -> liquidation 9.52% away = 9.5 gold ATR, 4.8 ETH ATR
        25x  ->             3.49%      = 3.5           1.7
        50x  ->             1.48%      = 1.5           0.7
       100x  ->             0.47%      = 0.5           0.2

    The distance is (1/L - mm) / (1 - mm), not 1/L: the maintenance margin is
    subtracted, which bites hard at high leverage and sends the distance to
    zero at L = 1/mm, about 189x. At 100x on ether, liquidation is a fifth of
    an average day away. So the ceiling here
    is set by volatility rather than by a fixed number: liquidation must stay
    at least `min_liquidation_atr` average moves away, which lets gold carry
    more leverage than ether for exactly the reason it should.

    One caution the code enforces rather than assumes. Margin already scales
    with confidence, so scaling leverage too makes exposure grow QUADRATICALLY
    — 65% confidence gives Rs501 at 10x = Rs5,010 of notional, while 85% gives
    Rs1,500 at 25x = Rs37,500. That is seven and a half times the exposure for
    twenty points of confidence. max_notional_pct_of_wallet caps it.
    """

    floor_leverage: float = 10.0        # never lower — your stated floor
    ceiling_leverage: float = 25.0
    floor_confidence: float = 0.65
    ceiling_confidence: float = 0.85

    # Liquidation must survive this many average moves. Three means an
    # ordinary day cannot reach it, and a violent one still might.
    min_liquidation_atr: float = 3.0

    # Ceiling on margin x leverage, as a multiple of the wallet. Without it,
    # confidence compounds through both terms at once.
    max_notional_pct_of_wallet: float = 12.0

    def leverage_for(self, confidence: float, atr_pct: float | None = None,
                     maintenance_margin_pct: float = 0.0053) -> float:
        """
        Leverage for this confidence, reduced if volatility cannot support it.

        Returns the floor rather than zero when volatility is hostile: the
        decision to skip the trade belongs to the signal gate, not here.
        """
        span = self.ceiling_confidence - self.floor_confidence
        frac = 0.0 if span <= 0 else (confidence - self.floor_confidence) / span
        frac = max(0.0, min(1.0, frac))
        lev = self.floor_leverage + frac * (self.ceiling_leverage - self.floor_leverage)

        if atr_pct and atr_pct > 0:
            # Solve (1/L - mm)/(1 - mm) >= k*atr for L:
            #   L <= 1 / (k*atr*(1 - mm) + mm)
            # Dropping the mm term overstates the safe leverage badly — at
            # 100x it claims 1.01% of room where there is 0.47%.
            denom = self.min_liquidation_atr * atr_pct * (1 - maintenance_margin_pct)
            denom += maintenance_margin_pct
            lev = min(lev, 1.0 / denom) if denom > 0 else lev
        return max(self.floor_leverage, lev)

    def notional_cap(self, wallet: float) -> float:
        return wallet * self.max_notional_pct_of_wallet


@dataclass(frozen=True)
class SizingConfig:
    """
    Position size scales with conviction, so a strong setup gets more capital
    than a marginal one.

    Example on a Rs3,000 wallet with the defaults below:

        confidence 65%  ->  Rs500   (weak, minimum size)
        confidence 75%  ->  Rs1,000 (decent)
        confidence 85%  ->  Rs1,500 (strong, maximum size)

    Sizes interpolate linearly between floor and ceiling across the confidence
    band, then get clipped so the open book never exceeds max_total_exposure_pct
    of the wallet. Without that cap, three strong signals at once would commit
    every rupee you have — and three simultaneous stops would take a fifth of
    the wallet in one move.
    """

    # Share of wallet at the bottom and top of the confidence band.
    floor_margin_pct: float = 0.167      # ~Rs500 of Rs3,000
    ceiling_margin_pct: float = 0.50     # ~Rs1,500 of Rs3,000

    # Confidence range the scale is stretched across.
    floor_confidence: float = 0.65
    ceiling_confidence: float = 0.85

    min_margin: float = 50.0             # below this a trade is not worth the fee
    max_total_exposure_pct: float = 1.00  # cap on all open margin combined

    def margin_for(self, wallet: float, confidence: float,
                   already_committed: float = 0.0) -> float:
        """
        Margin for one trade, given conviction and what is already at risk.

        Returns 0.0 when the trade cannot be funded — the caller should skip
        rather than open something too small to overcome its own fee.
        """
        span = self.ceiling_confidence - self.floor_confidence
        if span <= 0:
            frac = 1.0
        else:
            frac = (confidence - self.floor_confidence) / span
        frac = max(0.0, min(1.0, frac))

        pct = self.floor_margin_pct + frac * (self.ceiling_margin_pct - self.floor_margin_pct)
        margin = wallet * pct

        # Respect the total-exposure ceiling across everything already open.
        room = wallet * self.max_total_exposure_pct - already_committed
        margin = min(margin, room, wallet)

        return margin if margin >= self.min_margin else 0.0


@dataclass(frozen=True)
class ReviewConfig:
    """
    Rules for re-checking a position after it has been opened.

    A stop-loss only answers "has price moved against me". It cannot answer
    "is the reason I opened this trade still true". Those are different
    questions, and the second one often turns false well before the stop is
    reached — the setup decays, or the market breaks regime entirely.

    Reviewing costs a round-trip fee every time it fires, so the thresholds
    are deliberately conservative: this should catch genuine regime breaks,
    not noise. An over-eager reviewer converts small wins into fee churn.

    MEASURED RESULT — default is OFF, deliberately.
    Backtested across random, trending and regime-change (calm-then-crash)
    price series, enabling this never improved net P&L:

        scenario        review OFF   default ON   shock-only + 30m hold
        random          -Rs161       -Rs497       -Rs161  (never fired)
        calm -> crash   -Rs842       -Rs900       -Rs842  (never fired)

    The reason is instructive: the stop-loss already handles a crash. By the
    time a review can tell you the setup has broken, price is usually near the
    stop anyway, so the reviewer exits at a similar level and pays an extra
    round trip for the privilege. The aggressive default nearly doubled trade
    count (62 -> 119) purely in churn.

    It is kept because synthetic data cannot represent a genuine news shock —
    the case it was built for — so it is worth A/B testing on real history
    before dismissing. Turn it on, run the same window both ways, and let the
    numbers decide. If you do enable it, prefer the shock-only preset below,
    which was the only variant that never fired spuriously.
    """

    enabled: bool = False

    # How often to re-run the analyzers against an open position.
    normal_interval_minutes: int = 60
    # Cadence used once the market is judged to be moving violently.
    fast_interval_minutes: int = 1
    # How long fast mode persists after the last shock.
    fast_mode_duration_minutes: int = 30

    # Close if the setup now points the opposite way. Off by default: with
    # mean-reversion analyzers the signal naturally flips as price recovers,
    # so this fires constantly and just pays fees.
    exit_on_direction_flip: bool = False
    # Close if conviction decays below this. None disables the check.
    exit_confidence_floor: float | None = None
    # Don't act on a review early on — the entry bar itself frequently
    # re-triggers the very analyzer that produced the position.
    min_hold_minutes_before_review: int = 30

    # What counts as a "major thing happening". Both are relative to the
    # instrument's own recent behaviour, not absolute numbers, so they travel
    # across coins with very different volatility.
    shock_atr_multiple: float = 2.5      # candle range vs ATR-14
    shock_volume_multiple: float = 3.0   # candle volume vs recent average
    # Close immediately on a shock that is moving against the position by
    # more than this share of the distance to the stop.
    shock_adverse_stop_fraction: float = 0.75

    @classmethod
    def shock_only(cls) -> ReviewConfig:
        """Safest tested preset: react only to violent bars, never to signal drift."""
        return cls(enabled=True, exit_on_direction_flip=False,
                   exit_confidence_floor=None, min_hold_minutes_before_review=30)

    @classmethod
    def aggressive(cls) -> ReviewConfig:
        """Reacts to signal drift too. Measured to churn fees — A/B before trusting."""
        return cls(enabled=True, exit_on_direction_flip=True,
                   exit_confidence_floor=0.50, normal_interval_minutes=15,
                   min_hold_minutes_before_review=5)


def is_market_shock(candle_range: float, atr: float,
                    volume: float, avg_volume: float,
                    cfg: ReviewConfig) -> bool:
    """True if this bar looks like a genuine event rather than normal noise."""
    range_shock = atr > 0 and candle_range >= atr * cfg.shock_atr_multiple
    volume_shock = avg_volume > 0 and volume >= avg_volume * cfg.shock_volume_multiple
    return bool(range_shock or volume_shock)


def adverse_fraction_of_stop(pos: Position, price: float) -> float:
    """
    How far price has travelled toward the stop, as a fraction.

    0.0 = at entry, 1.0 = at the stop. Above 1.0 the stop would already have
    triggered. Used to decide whether a shock is threatening enough to exit on.
    """
    span = abs(pos.entry_price - pos.stop_price)
    if span <= 0:
        return 0.0
    if pos.side is Side.LONG:
        moved = pos.entry_price - price
    else:
        moved = price - pos.entry_price
    return max(0.0, moved / span)


@dataclass
class CycleConfig:
    """One run of the simulator, start to finish."""

    starting_wallet: float = 1000.0
    target_wallet: float = 20000.0
    leverage: float = 10.0
    margin_per_trade_pct: float = 0.20      # of current wallet
    min_margin: float = 50.0
    stop_pct_of_margin: float = 0.20        # risk 20% of margin per trade
    reward_risk: float = 2.0                # target sits 2x the stop distance away
    min_confidence: float = 0.70
    max_concurrent: int = 3          # across all symbols; one position per symbol
    max_hold_minutes: int = 240
    fees: FeeModel = field(default_factory=FeeModel)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    sizing: SizingConfig | None = None   # None = flat margin_per_trade_pct

    # None = the fixed `leverage` above. Set it to scale leverage with
    # confidence, bounded by volatility.
    leverage_scaling: LeverageConfig | None = None

    # How many times the round-trip cost a target must clear.
    #
    # This is the SAME question the analyzers answer with
    # ScalpConfig.min_edge_multiple, so it takes its default from there rather
    # than carrying a second number. Two floors meant a dead band: with the
    # analyzer at 2.0 and the engine at 3.0, anything between 0.336% and
    # 0.354% was published to the dashboard and then refused by the engine —
    # a signal you could see and the bot would never take.
    #
    # The value itself follows from kept = 1 - 1/x: at 2.0 a trade keeps half
    # its gross, at 3.0 two thirds, at 5.0 four fifths.
    min_target_to_fee_ratio: float = field(
        default_factory=lambda: ScalpConfig().min_edge_multiple)

    # Quote conversion for INR-margined futures on a USDT-priced pair.
    # usdt_inr=1.0 with lot_step=0.0 keeps prices and margin in one currency
    # with continuous sizing, which is what the historical backtest wants.
    # For a live CoinDCX cycle set the rate and the instrument's lot step so
    # the simulated fill size matches what the exchange would actually give.
    usdt_inr: float = 1.0
    lot_step: float = 0.0

    # Fills land near the quoted price, not on it. NO_SLIPPAGE reproduces the
    # old perfect-fill behaviour when you want to isolate its effect.
    slippage: SlippageModel = field(default_factory=SlippageModel)

    # Off by default. Turning it on changes the exit distribution, so it should
    # be a measured decision rather than an assumption.
    trailing: TrailingStop = field(default_factory=TrailingStop)
    ladder: ProfitLadder = field(default_factory=ProfitLadder)

    # How many closed bars of drift feed the trend term. Three bars is enough
    # to tell a run from a single spike without lagging into irrelevance.
    drift_lookback: int = 3

    # The most one stopped-out trade may cost, as a share of the margin posted
    # for it. This is the risk budget, and it is deliberately the thing that is
    # configured rather than the leverage: leverage is then whatever satisfies
    # it at the stop distance the market asked for. Four percent reproduces
    # what the first cycle actually risked per trade (10x against a 0.408%
    # stop), so widening the stop changes where the exit sits without changing
    # what a failure costs.
    max_loss_pct_of_margin: float = 0.04

    # A floor, so an unusually wide stop cannot reduce leverage to the point
    # where the position is too small to clear one lot.
    min_leverage: float = 1.0

    def is_target_viable(self, entry: float, target: float) -> bool:
        """Can this trade pay for itself if it works? If not, don't open it."""
        if entry <= 0 or target <= 0:
            return False
        move = abs(target - entry) / entry
        return move >= self.fees.round_trip_pct() * self.min_target_to_fee_ratio

    def leverage_for_signal(self, confidence: float,
                            atr_pct: float | None = None) -> float:
        """Confidence-scaled leverage when configured, else the fixed value."""
        if self.leverage_scaling is None:
            return self.leverage
        return self.leverage_scaling.leverage_for(
            confidence, atr_pct, self.fees.maintenance_margin_pct)

    def leverage_for_stop(self, leverage: float, entry: float,
                          stop_price: float, costs_pct: float = 0.0) -> float:
        """
        Lower the leverage until being stopped out costs no more than
        `max_loss_pct_of_margin`.

        This is the half of "widen the stop" that is easy to leave out and
        expensive to forget. What a stop-out costs is leverage times the price
        distance — 10x against a 0.408% stop is 4.1% of margin. Move the stop
        to 0.918% and leave the leverage alone and the same trade now loses
        9.2%, so a change made to survive noise would have doubled the loss on
        every trade that fails. That is the opposite of the instruction.

        Tying the two together means the risk budget is the thing that is
        actually configured, and the stop distance is free to follow the
        market. A quiet hour produces a tight stop and more leverage; a violent
        one produces a wide stop and less. The amount at stake does not move.

        Only ever reduces. A generous stop is not a reason to take more
        leverage than was asked for.

        `costs_pct` is what a stop-out costs on top of the distance: the
        round-trip fee, the spread both ways and the stop's own slippage.
        Leaving it out made a "4%" loss really 4.8-6.7% of margin.
        """
        if entry <= 0 or stop_price <= 0 or leverage <= 0:
            return leverage
        stop_move = abs(entry - stop_price) / entry
        if stop_move <= 0:
            return leverage
        affordable = self.max_loss_pct_of_margin / (stop_move + max(0.0, costs_pct))
        return max(self.min_leverage, min(leverage, affordable))

    def cap_margin_to_notional(self, margin: float, leverage: float,
                               wallet: float) -> float:
        """
        Hold total exposure under the cap.

        Margin and leverage both rise with confidence, so notional grows with
        the product. Without this a twenty-point confidence increase multiplies
        exposure sevenfold rather than doubling it.
        """
        if self.leverage_scaling is None or leverage <= 0:
            return margin
        cap = self.leverage_scaling.notional_cap(wallet)
        return min(margin, cap / leverage)

    def margin_for_signal(self, wallet: float, confidence: float,
                          already_committed: float = 0.0) -> float:
        """Confidence-scaled size when sizing is configured, else the flat size."""
        if self.sizing is not None:
            return self.sizing.margin_for(wallet, confidence, already_committed)
        flat = max(self.min_margin, wallet * self.margin_per_trade_pct)
        return flat if flat <= wallet and flat >= self.min_margin else 0.0

    def break_even_move_pct(self) -> float:
        return self.fees.round_trip_pct() * 100.0

    def stop_move_pct(self) -> float:
        return self.stop_pct_of_margin / self.leverage * 100.0

    def target_move_pct(self) -> float:
        return self.stop_move_pct() * self.reward_risk

    def liquidation_move_pct(self) -> float:
        """Approximate adverse move that triggers liquidation, in percent."""
        return (1.0 / self.leverage - self.fees.maintenance_margin_pct) * 100.0

    def sanity_report(self) -> dict:
        """Is this configuration even capable of making money? Checked before a run."""
        be = self.break_even_move_pct()
        tgt = self.target_move_pct()
        return {
            "break_even_move_pct": round(be, 4),
            "target_move_pct": round(tgt, 4),
            "stop_move_pct": round(self.stop_move_pct(), 4),
            "liquidation_move_pct": round(self.liquidation_move_pct(), 4),
            "target_clears_fees": tgt > be,
            "target_to_fee_ratio": round(tgt / be, 2) if be else None,
            "stop_inside_liquidation": self.stop_move_pct() < self.liquidation_move_pct(),
            "trailing_can_activate": self.trailing_can_activate(),
            "ladder_can_activate": self.ladder_can_activate(),
            "ladder_rungs_reachable": self.ladder_rungs_reachable(),
            "roe_at_target_pct": round(self.stop_pct_of_margin * self.reward_risk * 100, 2),
            "max_leverage_for_this_roe": round(self.max_leverage_for_roe(), 1),
            "roe_target_viable": self.roe_target_is_viable(),
        }

    def ladder_can_activate(self) -> bool:
        """
        Can any rung fire before the target closes the trade?

        The target sits at stop_pct_of_margin * reward_risk of ROE. A first
        rung at or above that is unreachable — the position is already closed.
        With a fixed 20/20 the target is +20% ROE and the default ladder's
        first rung is also +20%, so it fires only on a bar that overshoots.
        """
        if not self.ladder.enabled or not self.ladder.rungs:
            return False
        return self.ladder.rungs[0][0] < self.stop_pct_of_margin * self.reward_risk

    def ladder_rungs_reachable(self) -> int:
        """How many rungs the target leaves room for. Zero means it is inert."""
        if not self.ladder.enabled:
            return 0
        target_roe = self.stop_pct_of_margin * self.reward_risk
        return sum(1 for reached, _ in self.ladder.rungs if reached < target_roe)

    def roe_target_is_viable(self) -> bool:
        """
        Is the target still a big enough PRICE move at this leverage?

        The target is set as a percentage of margin, but the fee is a
        percentage of price. Those diverge as leverage rises, and past a
        certain point a fixed ROE target asks for less movement than the round
        trip costs — so hitting it loses money.
        """
        floor = self.fees.round_trip_pct() * self.min_target_to_fee_ratio
        return self.target_move_pct() / 100.0 >= floor

    def max_leverage_for_roe(self) -> float:
        """Leverage ceiling before this ROE target drops beneath its own costs."""
        floor = self.fees.round_trip_pct() * self.min_target_to_fee_ratio
        roe = self.stop_pct_of_margin * self.reward_risk
        return roe / floor if floor > 0 else float("inf")

    def trailing_can_activate(self) -> bool:
        """
        Can the trail ever fire under this configuration?

        The target sits at reward_risk R. If the trail only wakes at or beyond
        that, the position closes at the target first and the trail is dead
        code — which is exactly what happens on a fixed 20/20 (reward_risk 1.0)
        with the default activate_at_r of 1.0. Either activate earlier or let
        the target go.
        """
        if not self.trailing.enabled:
            return False
        # release_target cannot rescue this: it only takes effect ON
        # activation, so a threshold that is never reached stays never reached.
        return self.trailing.activate_at_r < self.reward_risk
