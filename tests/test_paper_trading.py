"""Tests for the leveraged futures maths and the backtest engine.

These pin down the behaviours that are easy to get subtly wrong and expensive
to get wrong silently: fees on notional, exact liquidation, pessimistic
resolution of ambiguous candles, and conservation of money.
"""
from __future__ import annotations

import math
import random
import unittest
from datetime import UTC, datetime, timedelta

from analysis.backtest import BacktestEngine
from analysis.paper_trading import (
    NO_SLIPPAGE,
    CycleConfig,
    ExitReason,
    FeeModel,
    Side,
    SlippageModel,
    TrailingStop,
    close_position,
    liquidation_price,
    open_position,
    resolve_candle,
    round_to_lot,
    stop_and_target,
)
from collectors.historical_klines import Candle

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def synthetic(n: int, seed: int, vol: float = 0.004, drift: float = 0.0,
              start: float = 62_000.0) -> list[Candle]:
    """
    Candles with a genuine high/low, unlike the flat ones REST polling makes.

    Anchored at a realistic BTC price because the tick gate is per-instrument:
    BTC's 0.1 tick is 0.1% of a $100 series, coarse enough to refuse setups
    that would be perfectly tradeable at the real price.
    """
    random.seed(seed)
    out, p = [], start
    for i in range(n):
        o = p
        c = o * (1 + random.gauss(drift, vol))
        wick = abs(random.gauss(0, vol * 0.6))
        out.append(Candle(T0 + timedelta(minutes=i), o,
                          max(o, c) * (1 + wick), min(o, c) * (1 - wick),
                          c, random.uniform(80, 400)))
        p = c
    return out


class TestFuturesMaths(unittest.TestCase):
    def setUp(self):
        self.f = FeeModel()

    def test_fees_charged_on_notional_not_margin(self):
        """At 10x the round trip costs ~1.18% of margin. This is the trap."""
        margin = 1000.0
        for lev in (1, 5, 10, 20):
            rt = self.f.entry_fee(margin * lev) + self.f.exit_fee(margin * lev)
            self.assertAlmostEqual(rt / margin, 2 * self.f.effective_taker_pct * lev, places=9)

    def test_break_even_move_is_leverage_independent(self):
        """Leverage multiplies gain and fee equally, so it never changes the sign."""
        self.assertAlmostEqual(self.f.round_trip_pct(), 2 * 0.0005 * 1.18, places=9)

    def test_gst_is_included_in_the_effective_rate(self):
        """18% GST on brokerage is unavoidable, so it belongs in the fee, not a footnote."""
        self.assertAlmostEqual(self.f.effective_taker_pct, 0.0005 * 1.18, places=9)
        self.assertGreater(self.f.effective_taker_pct, self.f.taker_pct)

    def test_liquidation_long_and_short(self):
        lp = liquidation_price(100.0, Side.LONG, 10, 0.015)
        sp = liquidation_price(100.0, Side.SHORT, 10, 0.015)
        self.assertAlmostEqual(lp, 100 * (1 - 0.1) / (1 - 0.015), places=9)
        self.assertAlmostEqual(sp, 100 * (1 + 0.1) / (1 + 0.015), places=9)
        self.assertLess(lp, 100)
        self.assertGreater(sp, 100)

    def test_higher_leverage_moves_liquidation_closer(self):
        prev = 0.0
        for lev in (5, 10, 20, 50):
            dist = 100 - liquidation_price(100.0, Side.LONG, lev, 0.015)
            self.assertGreater(dist, 0)
            if prev:
                self.assertLess(dist, prev)
            prev = dist

    def test_stop_and_target_derive_from_margin_risk(self):
        """stop 20% of margin at 10x is a 2% price move; R:R 2.0 puts target at 4%."""
        stop, target = stop_and_target(100.0, Side.LONG, 10, 0.20, 2.0)
        self.assertAlmostEqual(stop, 98.0, places=9)
        self.assertAlmostEqual(target, 104.0, places=9)

    def test_short_stop_and_target_are_mirrored(self):
        stop, target = stop_and_target(100.0, Side.SHORT, 10, 0.20, 2.0)
        self.assertAlmostEqual(stop, 102.0, places=9)
        self.assertAlmostEqual(target, 96.0, places=9)

    def test_ambiguous_candle_books_the_loss(self):
        """Both stop and target inside one candle: we cannot know the path, so assume the worst."""
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        reason, _ = resolve_candle(p, high=105.0, low=97.0, close=101.0, ts=T0)
        self.assertIs(reason, ExitReason.STOP)

    def test_stop_above_liquidation_fires_first(self):
        """
        Price reaches the higher level first on the way down, so a normal stop
        (98.0) fires before liquidation (~90.5) even though the bar ran through
        both. This assertion previously read the other way round, which invented
        liquidations that cannot physically happen.
        """
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        self.assertGreater(p.stop_price, p.liq_price)
        reason, price = resolve_candle(p, high=100.0, low=85.0, close=86.0, ts=T0)
        self.assertIs(reason, ExitReason.STOP)
        self.assertAlmostEqual(price, p.stop_price, places=9)

    def test_liquidation_fires_first_when_the_stop_is_below_it(self):
        """A stop wider than the account can survive never gets the chance."""
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 2.0, 2.0, T0)
        self.assertLess(p.stop_price, p.liq_price)
        reason, price = resolve_candle(p, high=100.0, low=85.0, close=86.0, ts=T0)
        self.assertIs(reason, ExitReason.LIQUIDATION)
        self.assertAlmostEqual(price, p.liq_price, places=9)

    def test_cannot_lose_more_than_margin(self):
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        t = close_position(p, 50.0, ExitReason.LIQUIDATION, T0, self.f, wallet_before=800.0)
        self.assertAlmostEqual(t.net_pnl, -200.0, places=9)
        self.assertAlmostEqual(t.wallet_after, 800.0, places=9)

    def test_short_profits_when_price_falls(self):
        p = open_position("X", Side.SHORT, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        reason, price = resolve_candle(p, high=101.0, low=95.5, close=96.0, ts=T0)
        self.assertIs(reason, ExitReason.TARGET)
        t = close_position(p, price, reason, T0, self.f, wallet_before=800.0)
        self.assertGreater(t.net_pnl, 0)

    def test_expiry_only_after_deadline(self):
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0,
                          expires_at=T0 + timedelta(hours=4))
        self.assertIsNone(resolve_candle(p, 100.5, 99.5, 100.2, T0 + timedelta(hours=1)))
        reason, price = resolve_candle(p, 100.5, 99.5, 100.2, T0 + timedelta(hours=4))
        self.assertIs(reason, ExitReason.EXPIRY)
        self.assertEqual(price, 100.2)

    def test_winning_trade_matches_hand_calculation(self):
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        t = close_position(p, 104.0, ExitReason.TARGET, T0, self.f, wallet_before=800.0)
        eff = self.f.effective_taker_pct
        self.assertAlmostEqual(t.gross_pnl, (104 - 100) * 20.0, places=9)
        # closed instantly, so no funding accrues; the target is a limit
        # order, so the exit pays maker (0.02% + GST), the entry taker
        maker = self.f.effective_maker_pct
        self.assertAlmostEqual(maker, 0.0002 * 1.18, places=12)
        self.assertAlmostEqual(t.fees_paid, 2000 * eff + 104 * 20 * maker, places=9)
        self.assertAlmostEqual(t.wallet_after, 800 + 200 + t.net_pnl, places=9)

    def test_only_the_target_exit_pays_maker(self):
        for reason, rate in ((ExitReason.STOP, self.f.effective_taker_pct),
                             (ExitReason.EXPIRY, self.f.effective_taker_pct),
                             (ExitReason.TARGET, self.f.effective_maker_pct)):
            p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
            t = close_position(p, 101.0, reason, T0, self.f, wallet_before=800.0)
            self.assertAlmostEqual(t.fees_paid, 2000 * self.f.effective_taker_pct
                                   + 101 * 20 * rate, places=9, msg=reason)
        self.assertAlmostEqual(self.f.round_trip_pct(), 0.00118, places=9)
        self.assertAlmostEqual(self.f.target_round_trip_pct(), 0.000826, places=9)

    def test_funding_accrues_with_time_held(self):
        """A position held for hours costs funding on top of the trading fees."""
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        quick = close_position(p, 100.0, ExitReason.EXPIRY, T0, self.f, 800.0)
        held = close_position(p, 100.0, ExitReason.EXPIRY,
                              T0 + timedelta(hours=24), self.f, 800.0)
        self.assertAlmostEqual(quick.funding_paid, 0.0, places=9)
        self.assertGreater(held.funding_paid, 0.0)
        self.assertGreater(held.fees_paid, quick.fees_paid)
        self.assertAlmostEqual(held.hours_held, 24.0, places=6)


class TestCalibrationAgainstRealTrades(unittest.TestCase):
    """
    Pins the cost model to figures observed on a real CoinDCX INR futures
    account on 18 Aug 2026. If CoinDCX changes its rates these will fail,
    which is exactly what should happen.
    """

    SIZE = 10681.44      # position notional, INR
    MARGIN = 533.31
    ENTRY = 1906.50
    OBSERVED_OPEN_FEE = 6.31
    OBSERVED_LIQ = 1820.97
    OBSERVED_FUNDING_16H = 1.43

    def setUp(self):
        self.f = FeeModel()

    def test_open_fee_matches_account(self):
        self.assertAlmostEqual(self.f.entry_fee(self.SIZE), self.OBSERVED_OPEN_FEE, delta=0.05)

    def test_liquidation_price_matches_account(self):
        lp = liquidation_price(self.ENTRY, Side.LONG, 20, self.f.maintenance_margin_pct)
        self.assertAlmostEqual(lp, self.OBSERVED_LIQ, delta=1.0)

    def test_funding_matches_account(self):
        self.assertAlmostEqual(self.f.funding_cost(self.SIZE, 16),
                               self.OBSERVED_FUNDING_16H, delta=0.25)

    def test_pnl_formula_matches_account(self):
        """qty x price move, converted at the implied USDT rate."""
        qty, ltp = 0.055, 1904.00
        usdt_inr = self.SIZE / (qty * self.ENTRY)
        pnl = qty * (ltp - self.ENTRY) * usdt_inr
        self.assertAlmostEqual(pnl, -14.03, delta=0.05)
        self.assertAlmostEqual(pnl / self.MARGIN * 100, -2.63, delta=0.05)

    def test_bch_close_fee_share_matches_ledger(self):
        """
        Real BCH close from the account: gross Rs63.42, fees Rs5.77, net Rs57.65.

        This pins the correction to an earlier over-broad claim that "fees are
        bigger than the profits". They are not, on trades sized like this one.
        The fee share of gross depends only on how far the target is, so the
        ledger fee implies a notional, and that notional implies the move.
        """
        gross, observed_fee = 63.42, 5.77
        implied_notional = observed_fee / self.f.round_trip_pct()
        self.assertAlmostEqual(implied_notional, 4890.0, delta=60.0)

        move_pct = gross / implied_notional
        self.assertGreater(move_pct, 0.012)          # a real 1.3% move
        self.assertLess(observed_fee / gross, 0.10)  # fees under a tenth of gross

    def test_fee_share_is_independent_of_leverage_and_size(self):
        """Leverage scales gross and fee alike; only target distance matters."""
        move = 0.0130
        shares = []
        for margin, lev in ((1000, 5), (1000, 10), (3000, 20)):
            notional = margin * lev
            shares.append(self.f.exit_fee(notional) * 2 / (notional * move))
        for s in shares[1:]:
            self.assertAlmostEqual(s, shares[0], places=9)
        self.assertAlmostEqual(shares[0], self.f.round_trip_pct() / move, places=9)


class TestConfigSanity(unittest.TestCase):
    def test_flags_targets_that_cannot_clear_fees(self):
        bad = CycleConfig(leverage=10, stop_pct_of_margin=0.20, reward_risk=0.05)
        self.assertFalse(bad.sanity_report()["target_clears_fees"])

    def test_accepts_a_workable_configuration(self):
        good = CycleConfig(leverage=10, stop_pct_of_margin=0.20, reward_risk=2.0)
        rep = good.sanity_report()
        self.assertTrue(rep["target_clears_fees"])
        self.assertTrue(rep["stop_inside_liquidation"])


class TestBacktestEngine(unittest.TestCase):
    def test_money_is_conserved(self):
        cfg = CycleConfig(starting_wallet=1000, leverage=10, min_confidence=0.60)
        r = BacktestEngine(cfg).run("BTCUSDT", synthetic(2000, seed=3))
        self.assertAlmostEqual(
            cfg.starting_wallet + sum(t.net_pnl for t in r.trades),
            r.final_wallet, places=6)

    def test_no_trade_opens_without_data(self):
        cfg = CycleConfig(starting_wallet=1000)
        r = BacktestEngine(cfg).run("BTCUSDT", [])
        self.assertEqual(r.ended_reason, "no_data")
        self.assertEqual(len(r.trades), 0)

    def test_signals_point_the_way_the_market_is_going(self):
        """
        Replaces two earlier attempts. The first asserted the engine profits in
        a mean-reverting market — an artefact of a divergence detector that
        fired on 51% of bars of noise. The second asserted it trades less in
        noise than in a trend, which stopped holding once the volume family was
        fixed and started voting.

        Directional accuracy is the claim that survives measurement: in a
        random walk there is no correct direction, so a signal count there
        proves nothing, but in a market with a known direction the signals
        should point that way.
        """
        def walk(n, seed, drift):
            random.seed(seed)
            out, p = [], 62_000.0
            for i in range(n):
                o = p
                p = o * (1 + drift + random.gauss(0, 0.005))
                w = abs(random.gauss(0, 0.003))
                out.append(Candle(T0 + timedelta(minutes=i), o,
                                  max(o, p) * (1 + w), min(o, p) * (1 - w),
                                  p, random.uniform(80, 400)))
            return out

        for drift, want in ((0.0015, "long"), (-0.0015, "short")):
            sides = []
            for seed in range(12):
                cfg = CycleConfig(starting_wallet=1000, leverage=10,
                                  reward_risk=2.0, min_confidence=0.60)
                r = BacktestEngine(cfg).run("BTCUSDT", walk(1500, seed, drift))
                sides += [t.position.side.value for t in r.trades]
            if not sides:
                continue
            with_trend = sum(1 for s in sides if s == want) / len(sides)
            self.assertGreater(with_trend, 0.70,
                               f"{with_trend:.0%} of {len(sides)} trades went {want}")

    def test_stop_is_respected_so_losses_stay_bounded(self):
        cfg = CycleConfig(starting_wallet=1000, leverage=10,
                          stop_pct_of_margin=0.20, min_confidence=0.60)
        r = BacktestEngine(cfg).run("BTCUSDT", synthetic(3000, seed=11, vol=0.006))
        for t in r.trades:
            if t.reason is ExitReason.STOP:
                # 20% stop plus round-trip fees, with a little slack for fill price
                self.assertGreater(t.return_on_margin, -0.35)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPositionReview(unittest.TestCase):
    """
    The adaptive exit: re-checking whether the reason for a trade is still true.

    These pin the mechanics (shock detection, distance-to-stop, rate limiting)
    and the deliberate choice to default the feature OFF, which is backed by
    measurement rather than taste.
    """

    def setUp(self):
        from analysis.paper_trading import ReviewConfig
        self.rc = ReviewConfig()
        self.f = FeeModel()

    def test_defaults_to_off_because_it_measured_worse(self):
        from analysis.paper_trading import ReviewConfig
        self.assertFalse(ReviewConfig().enabled)

    def test_shock_detects_range_and_volume_spikes(self):
        from analysis.paper_trading import ReviewConfig, is_market_shock
        rc = ReviewConfig()
        self.assertTrue(is_market_shock(3.0, 1.0, 100, 100, rc))   # 3x ATR range
        self.assertTrue(is_market_shock(1.0, 1.0, 400, 100, rc))   # 4x volume
        self.assertFalse(is_market_shock(1.0, 1.0, 100, 100, rc))  # calm
        self.assertFalse(is_market_shock(2.0, 1.0, 100, 100, rc))  # below threshold

    def test_shock_ignores_missing_baselines(self):
        """No ATR or no volume history must not read as a shock."""
        from analysis.paper_trading import ReviewConfig, is_market_shock
        rc = ReviewConfig()
        self.assertFalse(is_market_shock(5.0, 0.0, 0, 0, rc))

    def test_adverse_fraction_measures_distance_to_stop(self):
        from analysis.paper_trading import adverse_fraction_of_stop
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        self.assertAlmostEqual(adverse_fraction_of_stop(p, 100.0), 0.0, places=6)
        self.assertAlmostEqual(adverse_fraction_of_stop(p, 99.0), 0.5, places=6)
        self.assertAlmostEqual(adverse_fraction_of_stop(p, 98.0), 1.0, places=6)
        # a favourable move is not adverse
        self.assertAlmostEqual(adverse_fraction_of_stop(p, 102.0), 0.0, places=6)

    def test_adverse_fraction_mirrors_for_shorts(self):
        from analysis.paper_trading import adverse_fraction_of_stop
        p = open_position("X", Side.SHORT, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        self.assertAlmostEqual(adverse_fraction_of_stop(p, 101.0), 0.5, places=6)
        self.assertAlmostEqual(adverse_fraction_of_stop(p, 99.0), 0.0, places=6)

    def test_presets_are_configured_as_documented(self):
        from analysis.paper_trading import ReviewConfig
        safe = ReviewConfig.shock_only()
        self.assertTrue(safe.enabled)
        self.assertFalse(safe.exit_on_direction_flip)
        self.assertIsNone(safe.exit_confidence_floor)
        loud = ReviewConfig.aggressive()
        self.assertTrue(loud.exit_on_direction_flip)
        self.assertIsNotNone(loud.exit_confidence_floor)

    def test_shock_exit_fires_when_its_conditions_are_actually_met(self):
        """
        Tested directly rather than by hoping a synthetic run produces the
        coincidence of a shock bar AND an open position most of the way to its
        stop. The previous version asserted early_exits > 0 over a random
        series, which passed only because the old analyzers fired constantly.
        """
        from analysis.paper_trading import ReviewConfig, adverse_fraction_of_stop
        rc = ReviewConfig.aggressive()
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        near_stop = p.entry_price - (p.entry_price - p.stop_price) * 0.9
        self.assertGreaterEqual(adverse_fraction_of_stop(p, near_stop),
                                rc.shock_adverse_stop_fraction)

    def test_analyzer_driven_exits_need_the_analyzers_to_have_an_opinion(self):
        """
        SIGNAL_FLIP and CONVICTION_LOST both re-run the analyzers and compare.
        Since confluence gating means they usually abstain, those two exits are
        now largely inert — the code reads `no fresh read is not evidence
        against the position`, which is the right call but worth pinning so the
        behaviour is not mistaken for a wiring fault later.
        """
        from analysis.paper_trading import ReviewConfig
        cfg = CycleConfig(starting_wallet=1000, leverage=10, min_confidence=0.60,
                          review=ReviewConfig.aggressive())
        r = BacktestEngine(cfg).run("BTCUSDT", synthetic(6000, seed=7))
        self.assertGreater(len(r.trades), 0)
        self.assertGreaterEqual(r.early_exits, 0)

    def test_disabled_review_never_fires(self):
        from analysis.paper_trading import ReviewConfig
        cfg = CycleConfig(starting_wallet=1000, leverage=10, min_confidence=0.60,
                          review=ReviewConfig(enabled=False))
        r = BacktestEngine(cfg).run("BTCUSDT", synthetic(3000, seed=7))
        self.assertEqual(r.early_exits, 0)


class TestConcurrency(unittest.TestCase):
    def test_never_stacks_two_positions_in_one_symbol(self):
        """A single symbol must never hold two positions at once."""
        cfg = CycleConfig(starting_wallet=1000, leverage=10,
                          min_confidence=0.60, max_concurrent=3)
        r = BacktestEngine(cfg).run("BTCUSDT", synthetic(3000, seed=9))
        # every trade must close before the next one opens
        ordered = sorted(r.trades, key=lambda t: t.position.opened_at)
        for a, b in zip(ordered, ordered[1:]):
            self.assertLessEqual(a.closed_at, b.position.opened_at)


class TestConfidenceScaledSizing(unittest.TestCase):
    """Bigger positions for stronger signals, with a cap on total exposure."""

    def setUp(self):
        from analysis.paper_trading import SizingConfig
        self.sc = SizingConfig()

    def test_produces_the_requested_tiers(self):
        """On a Rs3,000 wallet: ~500 weak, ~1000 decent, ~1500 strong."""
        self.assertAlmostEqual(self.sc.margin_for(3000, 0.65), 500, delta=15)
        self.assertAlmostEqual(self.sc.margin_for(3000, 0.75), 1000, delta=15)
        self.assertAlmostEqual(self.sc.margin_for(3000, 0.85), 1500, delta=15)

    def test_size_increases_monotonically_with_confidence(self):
        prev = 0.0
        for c in (0.65, 0.70, 0.75, 0.80, 0.85):
            m = self.sc.margin_for(3000, c)
            self.assertGreaterEqual(m, prev)
            prev = m

    def test_clamps_outside_the_confidence_band(self):
        self.assertAlmostEqual(self.sc.margin_for(3000, 0.30),
                               self.sc.margin_for(3000, 0.65), delta=1)
        self.assertAlmostEqual(self.sc.margin_for(3000, 0.99),
                               self.sc.margin_for(3000, 0.85), delta=1)

    def test_scales_with_wallet(self):
        """Sizing is proportional, so the same rules work at 1k and at 20k."""
        small = self.sc.margin_for(1000, 0.85)
        big = self.sc.margin_for(10000, 0.85)
        self.assertAlmostEqual(big / small, 10.0, delta=0.1)

    def test_total_exposure_cap_refuses_over_commitment(self):
        from analysis.paper_trading import SizingConfig
        capped = SizingConfig(max_total_exposure_pct=0.60)
        committed = 0.0
        opened = 0
        for _ in range(5):
            m = capped.margin_for(3000, 0.85, committed)
            if m <= 0:
                break
            committed += m
            opened += 1
        self.assertGreater(opened, 0)
        self.assertLessEqual(committed, 3000 * 0.60 + 1)

    def test_refuses_a_position_too_small_to_be_worth_the_fee(self):
        self.assertEqual(self.sc.margin_for(100, 0.85, already_committed=99.0), 0.0)


class TestTargetViabilityFilter(unittest.TestCase):
    """Signals whose target cannot pay for the round trip must be refused."""

    def setUp(self):
        self.cfg = CycleConfig(leverage=10)

    def test_rejects_the_live_dashboard_signals(self):
        """Every signal observed on the live dashboard was unviable."""
        for entry, target in [(1902, 1903), (1905, 1905.19),
                              (1905, 1905.19), (1904, 1902)]:
            self.assertFalse(self.cfg.is_target_viable(entry, target),
                             f"{entry}->{target} should be refused")

    def test_accepts_a_target_that_clears_the_hurdle(self):
        # 1.0% away, comfortably past the ~0.18% requirement
        self.assertTrue(self.cfg.is_target_viable(1900.0, 1919.0))

    def test_rejects_degenerate_prices(self):
        self.assertFalse(self.cfg.is_target_viable(1905.0, 1905.0))
        self.assertFalse(self.cfg.is_target_viable(0.0, 100.0))

    def test_threshold_follows_the_fee_model(self):
        """Raise fees and more targets become unviable."""
        pricey = CycleConfig(leverage=10, fees=FeeModel(taker_pct=0.005))
        # 0.526% — comfortably over the 0.354% floor the 3.0x default sets.
        self.assertTrue(self.cfg.is_target_viable(1900.0, 1910.0))
        self.assertFalse(pricey.is_target_viable(1900.0, 1910.0))

    def test_the_real_trade_that_motivated_raising_the_default(self):
        """
        A 0.177% ETH move grossed Rs57.53 and paid Rs38.24 in fees, keeping
        Rs19.29. It is 0.118% x 1.5 to the digit, so the old 1.5x default let
        it through exactly. At 3.0x it is refused.
        """
        entry = 2000.0
        target = entry * 1.00177
        self.assertFalse(self.cfg.is_target_viable(entry, target))
        lenient = CycleConfig(leverage=10, min_target_to_fee_ratio=1.5)
        self.assertTrue(lenient.is_target_viable(entry, target))

    def test_a_fixed_roe_target_shrinks_as_leverage_rises(self):
        """
        The trap behind that trade: the target is a percentage of MARGIN, the
        fee is a percentage of PRICE. They diverge with leverage.
        """
        moves = []
        for lev in (10, 25, 50, 100):
            cfg = CycleConfig(leverage=lev, stop_pct_of_margin=0.20, reward_risk=1.0)
            moves.append(cfg.target_move_pct())
        self.assertEqual(moves, sorted(moves, reverse=True))
        self.assertAlmostEqual(moves[0], 2.0, places=6)    # 10x
        self.assertAlmostEqual(moves[-1], 0.2, places=6)   # 100x

    def test_a_fixed_roe_target_is_refused_past_the_leverage_ceiling(self):
        below = CycleConfig(leverage=50, stop_pct_of_margin=0.20, reward_risk=1.0)
        above = CycleConfig(leverage=100, stop_pct_of_margin=0.20, reward_risk=1.0)
        self.assertTrue(below.roe_target_is_viable())
        self.assertFalse(above.roe_target_is_viable())
        self.assertAlmostEqual(below.max_leverage_for_roe(), 56.5, delta=0.5)

    def test_engine_counts_what_it_refuses(self):
        cfg = CycleConfig(starting_wallet=1000, leverage=10, min_confidence=0.60,
                          reward_risk=0.02)   # absurdly tight targets
        r = BacktestEngine(cfg).run("BTCUSDT", synthetic(2000, seed=4))
        self.assertGreater(r.signals_rejected_unviable, 0)
        self.assertEqual(len(r.trades), 0, "no unviable trade should ever open")


class TestQuantityAgainstRealTrade(unittest.TestCase):
    """
    Reconciles the whole position against the ETH screenshot, not just the fee.

    Account showed: Rs533 margin, 20x, entry 1906.50, LTP 1904.00, 0.055 ETH,
    position size Rs10,681.44, open fee Rs6.31, liquidation 1820.97,
    P&L -Rs14.03 (-2.63% ROE).
    """

    # The rate that reconciles quantity, fee and P&L simultaneously. It sits
    # well above spot USD/INR because CoinDCX's INR futures carry a premium.
    RATE = 102.005
    LOT = 0.001

    def setUp(self):
        self.f = FeeModel()
        self.pos = open_position(
            "ETHUSDT", Side.LONG, 1906.50, 533.0, 20, self.f, 0.20, 2.0,
            datetime(2026, 8, 18), usdt_inr=self.RATE, lot_step=self.LOT,
        )

    def test_coin_quantity_matches_account(self):
        self.assertAlmostEqual(self.pos.coin_qty, 0.055, places=6)

    def test_naive_notional_over_price_is_wrong(self):
        """The bug this replaced: INR notional / USDT price is 100x too big."""
        naive = self.pos.target_notional / self.pos.entry_price
        self.assertGreater(naive / self.pos.coin_qty, 90)

    def test_position_size_is_marked_to_ltp(self):
        self.assertAlmostEqual(self.pos.mark_notional(1904.00), 10681.44, delta=1.0)

    def test_open_fee_matches_account(self):
        self.assertAlmostEqual(self.pos.entry_fee, 6.31, delta=0.02)

    def test_pnl_and_roe_match_account(self):
        pnl = self.pos.gross_pnl(1904.00)
        self.assertAlmostEqual(pnl, -14.03, delta=0.02)
        self.assertAlmostEqual(pnl / self.pos.margin * 100, -2.63, delta=0.02)

    def test_lot_rounding_is_to_nearest_not_truncated(self):
        """Raw size is 0.054797; truncation would have shown 0.054, not 0.055."""
        raw = self.pos.target_notional / (self.RATE * self.pos.entry_price)
        self.assertLess(raw, 0.055)
        self.assertEqual(round_to_lot(raw, self.LOT), 0.055)


class TestScaleInScaleOut(unittest.TestCase):
    """Double-checks on increasing and decreasing an open position."""

    RATE, LOT = 102.005, 0.001

    def _pos(self, side=Side.LONG):
        return open_position(
            "ETHUSDT", side, 2000.00, 1000.0, 10, FeeModel(), 0.20, 2.0,
            datetime(2026, 8, 18), usdt_inr=self.RATE, lot_step=self.LOT,
        )

    # ---- increment -----------------------------------------------------
    def test_increase_averages_the_entry(self):
        p = self._pos()
        q0 = p.coin_qty
        p.increase(1000.0, 1900.00, FeeModel(), 0.20, 2.0)
        added = p.coin_qty - q0
        expected = (q0 * 2000.00 + added * 1900.00) / p.coin_qty
        self.assertAlmostEqual(p.entry_price, expected, places=6)
        self.assertLess(p.entry_price, 2000.00)   # averaged down
        self.assertGreater(p.entry_price, 1900.00)

    def test_increase_adds_margin_and_quantity(self):
        p = self._pos()
        q0, m0 = p.coin_qty, p.margin
        p.increase(500.0, 2000.00, FeeModel(), 0.20, 2.0)
        self.assertAlmostEqual(p.margin, m0 + 500.0, places=6)
        # Half the margin added, so about half the coins — but each fill is
        # snapped to a lot independently, so allow one lot of slack.
        self.assertAlmostEqual(p.coin_qty, q0 * 1.5, delta=self.LOT)

    def test_increase_charges_fee_only_on_the_added_notional(self):
        f = FeeModel()
        p = self._pos()
        before = p.entry_fee
        added_fee = p.increase(500.0, 2000.00, f, 0.20, 2.0)
        self.assertAlmostEqual(p.entry_fee, before + added_fee, places=9)
        # Half the original size added -> about half the original fee, within
        # the one-lot rounding on the added leg.
        one_lot_fee = f.entry_fee(self.LOT * 2000.00 * self.RATE)
        self.assertAlmostEqual(added_fee, before * 0.5, delta=one_lot_fee)

    def test_increase_moves_stop_target_and_liquidation_to_new_entry(self):
        f = FeeModel()
        p = self._pos()
        old_stop, old_liq = p.stop_price, p.liq_price
        p.increase(1000.0, 1900.00, f, 0.20, 2.0)
        self.assertLess(p.stop_price, old_stop)
        self.assertLess(p.liq_price, old_liq)
        self.assertAlmostEqual(
            p.liq_price,
            liquidation_price(p.entry_price, Side.LONG,
                              p.effective_leverage, f.maintenance_margin_pct),
            places=6,
        )

    def test_increase_below_one_lot_is_refused(self):
        p = self._pos()
        q0, m0 = p.coin_qty, p.margin
        self.assertEqual(p.increase(0.05, 2000.00, FeeModel(), 0.20, 2.0), 0.0)
        self.assertEqual(p.coin_qty, q0)
        self.assertEqual(p.margin, m0)

    # ---- decrement -----------------------------------------------------
    def test_reduce_leaves_leverage_and_liquidation_untouched(self):
        p = self._pos()
        lev, liq, entry = p.effective_leverage, p.liq_price, p.entry_price
        p.reduce(p.coin_qty / 2, 2100.00, FeeModel())
        self.assertAlmostEqual(p.effective_leverage, lev, places=6)
        self.assertAlmostEqual(p.liq_price, liq, places=6)
        self.assertAlmostEqual(p.entry_price, entry, places=9)

    def test_reduce_frees_margin_pro_rata(self):
        p = self._pos()
        m0, q0 = p.margin, p.coin_qty
        _, _, freed = p.reduce(q0 * 0.4, 2100.00, FeeModel())
        share = (q0 - p.coin_qty) / q0
        self.assertAlmostEqual(freed, m0 * share, places=6)
        self.assertAlmostEqual(p.margin, m0 - freed, places=6)

    def test_two_half_exits_equal_one_full_exit(self):
        """Scaling out in two steps must not create or destroy money."""
        f = FeeModel()
        whole = self._pos()
        g_whole = whole.gross_pnl(2100.00)
        f_whole = f.exit_fee(whole.mark_notional(2100.00))

        p = self._pos()
        half = round_to_lot(p.coin_qty / 2, self.LOT)
        g1, f1, _ = p.reduce(half, 2100.00, f)
        g2, f2, _ = p.reduce(p.coin_qty, 2100.00, f)
        self.assertAlmostEqual(g1 + g2, g_whole, places=6)
        self.assertAlmostEqual(f1 + f2, f_whole, places=6)
        self.assertAlmostEqual(p.coin_qty, 0.0, places=9)

    def test_reduce_short_realises_the_opposite_sign(self):
        f = FeeModel()
        long_, short = self._pos(Side.LONG), self._pos(Side.SHORT)
        g_long, _, _ = long_.reduce(long_.coin_qty, 2100.00, f)
        g_short, _, _ = short.reduce(short.coin_qty, 2100.00, f)
        self.assertGreater(g_long, 0)
        self.assertAlmostEqual(g_short, -g_long, places=6)

    def test_reduce_more_than_held_closes_the_position_only(self):
        p = self._pos()
        g, _, freed = p.reduce(p.coin_qty * 10, 2100.00, FeeModel())
        self.assertAlmostEqual(p.coin_qty, 0.0, places=9)
        self.assertAlmostEqual(p.margin, 0.0, places=6)
        self.assertAlmostEqual(freed, 1000.0, places=6)

    def test_round_trip_scale_in_then_full_out_conserves_money(self):
        f = FeeModel()
        p = self._pos()
        spent = p.entry_fee
        spent += p.increase(1000.0, 2000.00, f, 0.20, 2.0)
        g, fee, freed = p.reduce(p.coin_qty, 2000.00, f)
        self.assertAlmostEqual(g, 0.0, places=6)          # no price move
        self.assertAlmostEqual(freed, 2000.0, places=6)   # both margins back
        # Two opens on Rs10,000 of notional each, then one close on the
        # combined Rs20,000 — four units of the same fee, and nothing else.
        one_open = f.entry_fee(1000.0 * 10)
        self.assertAlmostEqual(spent + fee, 4 * one_open, delta=0.5)


class TestSlippage(unittest.TestCase):
    """
    "if eth is 1899 but not all trade execute at 1899 some will trigger at
    1898.2 also or 1901 also depending on trend"
    """

    REF = 1899.0

    def setUp(self):
        self.s = SlippageModel()

    def test_rising_market_fills_a_buy_above_the_quote(self):
        fill = self.s.entry_fill(self.REF, Side.LONG, drift_pct=0.0035)
        self.assertGreater(fill, self.REF)

    def test_falling_market_fills_a_buy_below_the_quote(self):
        """The trend term is signed by the market, so it helps as often as it hurts."""
        fill = self.s.entry_fill(self.REF, Side.LONG, drift_pct=-0.0035)
        self.assertLess(fill, self.REF)

    def test_flat_market_still_costs_the_spread(self):
        long_fill = self.s.entry_fill(self.REF, Side.LONG, drift_pct=0.0)
        short_fill = self.s.entry_fill(self.REF, Side.SHORT, drift_pct=0.0)
        self.assertGreater(long_fill, self.REF)   # buy lifts the ask
        self.assertLess(short_fill, self.REF)     # sell hits the bid
        self.assertAlmostEqual(long_fill - self.REF, self.REF - short_fill, places=9)

    def test_the_quoted_1899_lands_either_side(self):
        """Reproduces the exact example: 1899 becomes ~1898.2 or ~1901."""
        down = self.s.entry_fill(self.REF, Side.LONG, drift_pct=-0.0015)
        up = self.s.entry_fill(self.REF, Side.LONG, drift_pct=0.0037)
        self.assertAlmostEqual(down, 1898.2, delta=0.2)
        self.assertAlmostEqual(up, 1901.0, delta=0.3)

    def test_stop_always_fills_worse_than_the_level(self):
        for drift in (-0.004, 0.0, 0.004):
            long_stop = self.s.exit_fill(1860.0, Side.LONG, ExitReason.STOP, drift)
            short_stop = self.s.exit_fill(1940.0, Side.SHORT, ExitReason.STOP, drift)
            with self.subTest(drift=drift):
                self.assertLess(long_stop, 1860.0)     # sold lower than the stop
                self.assertGreater(short_stop, 1940.0)  # bought higher than the stop

    def test_stop_is_worse_than_an_ordinary_exit(self):
        stop = self.s.exit_fill(1860.0, Side.LONG, ExitReason.STOP, 0.0)
        expiry = self.s.exit_fill(1860.0, Side.LONG, ExitReason.EXPIRY, 0.0)
        self.assertLess(stop, expiry)

    def test_liquidation_is_worse_than_a_stop(self):
        liq = self.s.exit_fill(1800.0, Side.LONG, ExitReason.LIQUIDATION, 0.0)
        stop = self.s.exit_fill(1800.0, Side.LONG, ExitReason.STOP, 0.0)
        self.assertLess(liq, stop)

    def test_target_fills_exactly_at_the_limit(self):
        """A limit order fills at its price or not at all — never better."""
        for side in (Side.LONG, Side.SHORT):
            for drift in (-0.005, 0.0, 0.005):
                self.assertEqual(
                    self.s.exit_fill(1950.0, side, ExitReason.TARGET, drift), 1950.0
                )

    def test_slippage_is_capped(self):
        wild = self.s.entry_fill(self.REF, Side.LONG, drift_pct=0.90)
        self.assertAlmostEqual(wild, self.REF * (1 + self.s.max_slip_pct), places=6)

    def test_no_slippage_model_reproduces_perfect_fills(self):
        self.assertEqual(NO_SLIPPAGE.entry_fill(self.REF, Side.LONG, 0.02), self.REF)
        self.assertEqual(
            NO_SLIPPAGE.exit_fill(self.REF, Side.LONG, ExitReason.STOP, 0.02), self.REF
        )

    def test_it_is_deterministic(self):
        """No RNG anywhere — the same inputs must give bit-identical fills."""
        a = [self.s.entry_fill(self.REF, Side.LONG, d / 10000) for d in range(-50, 50)]
        b = [self.s.entry_fill(self.REF, Side.LONG, d / 10000) for d in range(-50, 50)]
        self.assertEqual(a, b)


class TestSlippageOnPositions(unittest.TestCase):
    """Slippage has to reach the position, not just sit in a helper."""

    def _open(self, drift, slip=None):
        return open_position(
            "ETHUSDT", Side.LONG, 1899.0, 1000.0, 10, FeeModel(), 0.20, 2.0,
            datetime(2026, 8, 18), slippage=slip if slip is not None else SlippageModel(),
            drift_pct=drift,
        )

    def test_entry_price_is_the_fill_not_the_quote(self):
        p = self._open(0.0035)
        self.assertEqual(p.signal_price, 1899.0)
        self.assertGreater(p.entry_price, 1899.0)

    def test_stop_and_target_are_measured_from_the_fill(self):
        """Anchoring risk to the quote would understate the loss we can take."""
        p = self._open(0.0035)
        stop_move = (p.entry_price - p.stop_price) / p.entry_price
        self.assertAlmostEqual(stop_move, 0.20 / 10, places=9)

    def test_entry_slippage_pct_is_signed_against_us(self):
        chased = self._open(0.0035)      # bought into a rally: bad
        favoured = self._open(-0.0035)   # bought into a slide: good
        self.assertGreater(chased.entry_slippage_pct, 0)
        self.assertLess(favoured.entry_slippage_pct, 0)

    def test_short_entry_slippage_sign_mirrors_long(self):
        p = open_position(
            "ETHUSDT", Side.SHORT, 1899.0, 1000.0, 10, FeeModel(), 0.20, 2.0,
            datetime(2026, 8, 18), slippage=SlippageModel(), drift_pct=-0.0035,
        )
        self.assertLess(p.entry_price, 1899.0)      # sold lower
        self.assertGreater(p.entry_slippage_pct, 0)  # which is against us

    def test_resolve_candle_slips_the_stop_but_not_the_target(self):
        p = self._open(0.0, NO_SLIPPAGE)
        s = SlippageModel()
        reason, price = resolve_candle(p, p.entry_price, p.stop_price - 1, p.stop_price,
                                       datetime(2026, 8, 18), slippage=s, drift_pct=0.0)
        self.assertIs(reason, ExitReason.STOP)
        self.assertLess(price, p.stop_price)

        reason, price = resolve_candle(p, p.target_price + 1, p.entry_price, p.target_price,
                                       datetime(2026, 8, 18), slippage=s, drift_pct=0.0)
        self.assertIs(reason, ExitReason.TARGET)
        self.assertEqual(price, p.target_price)

    def test_slippage_makes_a_round_trip_cost_more(self):
        f = FeeModel()
        clean = self._open(0.0, NO_SLIPPAGE)
        slipped = self._open(0.0)
        # Same quote, same stop distance, but the slipped entry is higher and
        # its stop fills lower, so the realised loss is strictly larger.
        _, clean_px = resolve_candle(clean, clean.entry_price, clean.stop_price - 1,
                                     clean.stop_price, datetime(2026, 8, 18),
                                     slippage=NO_SLIPPAGE)
        _, slip_px = resolve_candle(slipped, slipped.entry_price, slipped.stop_price - 1,
                                    slipped.stop_price, datetime(2026, 8, 18),
                                    slippage=SlippageModel())
        a = close_position(clean, clean_px, ExitReason.STOP, datetime(2026, 8, 18), f, 1000.0)
        b = close_position(slipped, slip_px, ExitReason.STOP, datetime(2026, 8, 18), f, 1000.0)
        self.assertLess(b.net_pnl, a.net_pnl)


class TestTrailingStop(unittest.TestCase):
    """
    Fixed 20% risk and 20% target, with the stop ratcheting forward once the
    trade is working — so a move that goes right and then reverses books a
    profit instead of a full loss.
    """

    ENTRY = 2000.0
    LEV = 10.0

    def _pos(self, side=Side.LONG, **kw):
        return open_position(
            "ETHUSDT", side, self.ENTRY, 1000.0, self.LEV, FeeModel(),
            0.20, 1.0, datetime(2026, 8, 18), slippage=NO_SLIPPAGE, **kw
        )

    def _trail(self, **kw):
        base = dict(enabled=True, activate_at_r=1.0, trail_pct_of_margin=0.20,
                    step_pct_of_margin=0.05, lock_breakeven=True)
        base.update(kw)
        return TrailingStop(**base)

    # ---- the 20/20 baseline -------------------------------------------
    def test_twenty_twenty_is_symmetric_around_entry(self):
        p = self._pos()
        self.assertAlmostEqual(self.ENTRY - p.stop_price, p.target_price - self.ENTRY, places=9)
        # 20% of margin at 10x is a 2% price move
        self.assertAlmostEqual((self.ENTRY - p.stop_price) / self.ENTRY, 0.02, places=9)

    def test_r_multiple_measures_from_the_original_risk(self):
        p = self._pos()
        self.assertAlmostEqual(p.r_multiple(self.ENTRY * 1.02), 1.0, places=6)
        self.assertAlmostEqual(p.r_multiple(self.ENTRY * 0.98), -1.0, places=6)

    # ---- activation ----------------------------------------------------
    def test_does_nothing_until_the_trade_is_up_one_r(self):
        p = self._pos()
        stop = p.stop_price
        self.assertFalse(p.update_trail(self.ENTRY * 1.015, self.ENTRY, self._trail(), FeeModel()))
        self.assertFalse(p.trail_active)
        self.assertEqual(p.stop_price, stop)

    def test_activates_at_one_r_and_locks_break_even(self):
        f = FeeModel()
        p = self._pos()
        self.assertTrue(p.update_trail(self.ENTRY * 1.02, self.ENTRY, self._trail(), f))
        self.assertTrue(p.trail_active)
        self.assertGreater(p.stop_price, self.ENTRY)   # a loss is no longer possible
        # Breakeven clears the FULL round trip: brokerage plus the spread and
        # slippage FeeModel does not know about. A stop that only covers the
        # fee still books a loss and calls it a scratch.
        t = self._trail()
        self.assertAlmostEqual(
            p.stop_price,
            self.ENTRY * (1 + f.round_trip_pct() + t.breakeven_buffer_pct),
            places=6)

    def test_disabled_trail_never_touches_the_stop(self):
        p = self._pos()
        stop = p.stop_price
        self.assertFalse(p.update_trail(self.ENTRY * 1.5, self.ENTRY, TrailingStop(), FeeModel()))
        self.assertEqual(p.stop_price, stop)

    # ---- the invariants ------------------------------------------------
    def test_the_stop_never_moves_backwards(self):
        """The invariant. A widening stop is not a risk control."""
        f, t, p = FeeModel(), self._trail(), self._pos()
        seen = [p.stop_price]
        for mult in (1.02, 1.06, 1.04, 1.09, 1.01, 1.12, 1.00, 1.05):
            p.update_trail(self.ENTRY * mult, self.ENTRY * (mult - 0.01), t, f)
            seen.append(p.stop_price)
        for a, b in zip(seen, seen[1:]):
            self.assertGreaterEqual(b, a)

    def test_short_stop_never_moves_backwards(self):
        f, t, p = FeeModel(), self._trail(), self._pos(Side.SHORT)
        seen = [p.stop_price]
        for mult in (0.98, 0.94, 0.96, 0.91, 0.99, 0.88, 1.00):
            p.update_trail(self.ENTRY * (mult + 0.01), self.ENTRY * mult, t, f)
            seen.append(p.stop_price)
        for a, b in zip(seen, seen[1:]):
            self.assertLessEqual(b, a)

    def test_stop_rides_the_configured_distance_behind_the_peak(self):
        f, p = FeeModel(), self._pos()
        p.update_trail(self.ENTRY * 1.10, self.ENTRY, self._trail(), f)
        # 20% of margin at 10x is a fixed 2%-of-ENTRY giveback, not 2% of the
        # peak. Quantity is fixed at entry, so a constant price distance is a
        # constant number of rupees — which is what "20% of margin" means.
        self.assertAlmostEqual(p.stop_price, self.ENTRY * 1.10 - self.ENTRY * 0.02, delta=0.5)

    def test_small_wiggles_do_not_rewrite_the_stop(self):
        f, t, p = FeeModel(), self._trail(), self._pos()
        p.update_trail(self.ENTRY * 1.10, self.ENTRY, t, f)
        stop = p.stop_price
        # a peak 0.1% higher is inside the 0.5% step, so nothing should move
        self.assertFalse(p.update_trail(self.ENTRY * 1.101, self.ENTRY, t, f))
        self.assertEqual(p.stop_price, stop)

    # ---- the point of the whole feature --------------------------------
    def test_a_runner_that_reverses_books_a_profit_instead_of_a_loss(self):
        f = FeeModel()
        t = self._trail()
        fixed, trailed = self._pos(), self._pos()

        # Runs to +6%, then collapses well through the original stop.
        for high in (self.ENTRY * 1.03, self.ENTRY * 1.06):
            trailed.update_trail(high, self.ENTRY, t, f)

        crash_low = self.ENTRY * 0.90
        a = resolve_candle(fixed, self.ENTRY * 1.06, crash_low, crash_low,
                           datetime(2026, 8, 18), slippage=NO_SLIPPAGE)
        b = resolve_candle(trailed, self.ENTRY * 1.06, crash_low, crash_low,
                           datetime(2026, 8, 18), slippage=NO_SLIPPAGE)
        self.assertIs(a[0], ExitReason.STOP)
        self.assertIs(b[0], ExitReason.STOP)

        fixed_t = close_position(fixed, a[1], a[0], datetime(2026, 8, 18), f, 1000.0)
        trail_t = close_position(trailed, b[1], b[0], datetime(2026, 8, 18), f, 1000.0)
        self.assertLess(fixed_t.net_pnl, 0)       # full 20% loss
        self.assertGreater(trail_t.net_pnl, 0)    # profit booked on the way down
        self.assertGreater(trail_t.net_pnl, fixed_t.net_pnl)

    def test_release_target_lets_a_winner_run(self):
        f, p = FeeModel(), self._pos()
        self.assertLess(p.target_price, math.inf)
        p.update_trail(self.ENTRY * 1.02, self.ENTRY, self._trail(release_target=True), f)
        self.assertEqual(p.target_price, math.inf)
        # the fixed target can no longer close it; only the trail can
        self.assertIsNone(resolve_candle(p, self.ENTRY * 1.50, self.ENTRY * 1.40,
                                         self.ENTRY * 1.45, datetime(2026, 8, 18),
                                         slippage=NO_SLIPPAGE))

    def test_short_side_mirrors(self):
        f, p = FeeModel(), self._pos(Side.SHORT)
        p.update_trail(self.ENTRY, self.ENTRY * 0.90, self._trail(), f)
        self.assertTrue(p.trail_active)
        self.assertLess(p.stop_price, self.ENTRY)
        self.assertAlmostEqual(p.stop_price, self.ENTRY * 0.90 + self.ENTRY * 0.02, delta=0.5)


class TestTrailingActivationSanity(unittest.TestCase):
    """
    Measured on the 20/20 baseline: trailing produced ZERO stop moves. The
    target sits at 1R and the trail woke at 1R, so every trade closed at the
    target before the trail could do anything. Worth a config check rather
    than a silent no-op.
    """

    def test_fixed_twenty_twenty_with_one_r_activation_is_dead_code(self):
        cfg = CycleConfig(leverage=10, stop_pct_of_margin=0.20, reward_risk=1.0,
                          trailing=TrailingStop(enabled=True, activate_at_r=1.0))
        self.assertFalse(cfg.trailing_can_activate())
        self.assertFalse(cfg.sanity_report()["trailing_can_activate"])

    def test_activating_earlier_makes_it_reachable(self):
        cfg = CycleConfig(leverage=10, stop_pct_of_margin=0.20, reward_risk=1.0,
                          trailing=TrailingStop(enabled=True, activate_at_r=0.5))
        self.assertTrue(cfg.trailing_can_activate())

    def test_a_wider_target_also_makes_it_reachable(self):
        cfg = CycleConfig(leverage=10, stop_pct_of_margin=0.20, reward_risk=2.0,
                          trailing=TrailingStop(enabled=True, activate_at_r=1.0))
        self.assertTrue(cfg.trailing_can_activate())

    def test_release_target_does_not_rescue_it(self):
        """release_target only applies once activated, so it cannot help."""
        cfg = CycleConfig(leverage=10, stop_pct_of_margin=0.20, reward_risk=1.0,
                          trailing=TrailingStop(enabled=True, activate_at_r=1.0,
                                                release_target=True))
        self.assertFalse(cfg.trailing_can_activate())

    def test_disabled_trailing_reports_false(self):
        self.assertFalse(CycleConfig().trailing_can_activate())
