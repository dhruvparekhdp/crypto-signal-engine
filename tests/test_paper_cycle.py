"""
The live cycle: does a wallet actually run, and does it stop when it should.

These tests drive the cycle the way the scheduler will — one price at a time,
with state round-tripping through the database between ticks, because that is
the path a redeploy takes.
"""
import unittest
from datetime import UTC, datetime, timedelta

from analysis.crypto_signal import CryptoSignal
from analysis.paper_cycle import (
    CycleState,
    committed_margin,
    cycle_outcome,
    fees_for,
    open_from_signal,
    resolve_at_price,
    should_open,
    summarise,
)
from analysis.paper_trading import (
    NO_SLIPPAGE,
    CycleConfig,
    ExitReason,
    SizingConfig,
    TrailingStop,
)

T0 = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def signal(symbol="ethusdt", direction="long", price=1906.5, confidence=0.80):
    return CryptoSignal(
        symbol=symbol, signal_type="test", direction=direction,
        trigger_description="x", confidence=confidence, current_price=price,
        target_price=price * 1.02, stop_loss=price * 0.98,
        edge_pct=1.0, stake_pct=1.0, timeframe="1h",
        sentiment_score=0.0, indicators_summary="", timestamp=T0,
    )


def config(**kw):
    base = dict(starting_wallet=3000.0, target_wallet=20000.0, leverage=10.0,
                stop_pct_of_margin=0.20, reward_risk=1.0, min_confidence=0.70,
                sizing=SizingConfig(), slippage=NO_SLIPPAGE)
    base.update(kw)
    return CycleConfig(**base)


def state(wallet=3000.0, positions=None):
    return CycleState(cycle_id=1, wallet=wallet, peak_wallet=wallet,
                      positions=positions or [], position_ids={})


class TestPerMarketFees(unittest.TestCase):
    def test_gold_and_ether_get_different_fee_models(self):
        eth, xau = fees_for("ethusdt"), fees_for("xauusdt")
        self.assertAlmostEqual(eth.effective_taker_pct * 100, 0.0590, places=4)
        self.assertAlmostEqual(xau.effective_taker_pct * 100, 0.0118, places=4)
        self.assertNotAlmostEqual(eth.maintenance_margin_pct, xau.maintenance_margin_pct)


class TestAdmission(unittest.TestCase):
    def test_low_confidence_is_refused(self):
        ok, why = should_open(signal(confidence=0.60), config(), state(), T0)
        self.assertFalse(ok)
        self.assertEqual(why, "confidence_below_floor")

    def test_one_position_per_symbol(self):
        cfg, st = config(), state()
        pos = open_from_signal(signal(), cfg, st, T0, 102.0)
        st.positions.append(pos)
        ok, why = should_open(signal(), cfg, st, T0)
        self.assertFalse(ok)
        self.assertEqual(why, "already_open_in_symbol")

    def test_a_different_symbol_is_allowed(self):
        cfg, st = config(), state()
        st.positions.append(open_from_signal(signal(), cfg, st, T0, 102.0))
        ok, _ = should_open(signal(symbol="btcusdt", price=62000.0), cfg, st, T0)
        self.assertTrue(ok)

    def test_concurrency_cap_is_enforced(self):
        cfg, st = config(max_concurrent=2), state()
        for sym, px in (("ethusdt", 1906.5), ("btcusdt", 62000.0)):
            st.positions.append(open_from_signal(signal(sym, price=px), cfg, st, T0, 102.0))
        ok, why = should_open(signal("solusdt", price=150.0), cfg, st, T0)
        self.assertFalse(ok)
        self.assertEqual(why, "max_concurrent")

    def test_exposure_cap_stops_over_committing(self):
        """Three high-confidence signals cannot all take their full size."""
        cfg, st = config(), state(wallet=3000.0)
        for sym, px in (("ethusdt", 1906.5), ("btcusdt", 62000.0), ("solusdt", 150.0)):
            p = open_from_signal(signal(sym, price=px, confidence=0.90), cfg, st, T0, 102.0)
            if p:
                st.positions.append(p)
        self.assertLessEqual(committed_margin(st.positions), 3000.0 + 1e-6)


class TestSizing(unittest.TestCase):
    def test_confidence_sets_the_ladder(self):
        cfg = config()
        for conf, expected in ((0.70, 751), (0.75, 1000), (0.85, 1500)):
            with self.subTest(conf=conf):
                pos = open_from_signal(signal(confidence=conf), cfg, state(), T0, 102.0)
                self.assertAlmostEqual(pos.margin, expected, delta=15)

    def test_quantity_uses_the_instrument_lot_step(self):
        pos = open_from_signal(signal(confidence=0.75), config(), state(), T0, 102.0)
        self.assertAlmostEqual(pos.coin_qty / 0.001, round(pos.coin_qty / 0.001), places=6)


class TestResolution(unittest.TestCase):
    def _pos(self, **kw):
        return open_from_signal(signal(**kw), config(), state(), T0, 102.0)

    def test_a_tick_at_the_target_closes_in_profit(self):
        cfg, pos = config(), self._pos()
        trade = resolve_at_price(pos, pos.target_price, T0 + timedelta(minutes=5), cfg, 3000.0)
        self.assertIsNotNone(trade)
        self.assertIs(trade.reason, ExitReason.TARGET)
        self.assertGreater(trade.net_pnl, 0)

    def test_a_tick_at_the_stop_closes_at_a_loss(self):
        cfg, pos = config(), self._pos()
        trade = resolve_at_price(pos, pos.stop_price, T0 + timedelta(minutes=5), cfg, 3000.0)
        self.assertIs(trade.reason, ExitReason.STOP)
        self.assertLess(trade.net_pnl, 0)

    def test_a_price_between_the_levels_keeps_it_open(self):
        cfg, pos = config(), self._pos()
        self.assertIsNone(resolve_at_price(pos, pos.entry_price, T0, cfg, 3000.0))

    def test_expiry_closes_a_stale_position(self):
        cfg, pos = config(), self._pos()
        late = pos.expires_at + timedelta(minutes=1)
        trade = resolve_at_price(pos, pos.entry_price, late, cfg, 3000.0)
        self.assertIs(trade.reason, ExitReason.EXPIRY)

    def test_the_trail_ratchets_on_a_favourable_tick(self):
        cfg = config(trailing=TrailingStop(enabled=True, activate_at_r=0.5))
        pos = open_from_signal(signal(), cfg, state(), T0, 102.0)
        start = pos.stop_price
        resolve_at_price(pos, pos.entry_price * 1.015, T0 + timedelta(minutes=1), cfg, 3000.0)
        self.assertGreater(pos.stop_price, start)
        self.assertTrue(pos.trail_active)


class TestCycleEnd(unittest.TestCase):
    def test_running_while_in_between(self):
        self.assertIsNone(cycle_outcome(5000.0, config(), open_count=0))

    def test_target_ends_the_cycle(self):
        self.assertEqual(cycle_outcome(20000.0, config(), open_count=0), "hit_target")

    def test_bust_ends_the_cycle(self):
        self.assertEqual(cycle_outcome(10.0, config(), open_count=0), "busted")

    def test_an_open_position_defers_the_bust_verdict(self):
        """Money is still on the table; the cycle is not over until it comes back."""
        self.assertIsNone(cycle_outcome(10.0, config(), open_count=1))


class TestScorecard(unittest.TestCase):
    def _run_to_completion(self, prices, cfg=None):
        """Drive one position through a price path, the way the scheduler will."""
        cfg = cfg or config()
        st = state()
        pos = open_from_signal(signal(), cfg, st, T0, 102.0)
        wallet = st.wallet - pos.margin
        for i, px in enumerate(prices):
            trade = resolve_at_price(pos, px, T0 + timedelta(minutes=i + 1), cfg, wallet)
            if trade:
                return trade
        return None

    def test_a_winning_path_produces_a_profitable_trade(self):
        pos_cfg = config()
        st = state()
        pos = open_from_signal(signal(), pos_cfg, st, T0, 102.0)
        trade = self._run_to_completion([pos.target_price])
        self.assertIsNotNone(trade)
        self.assertGreater(trade.net_pnl, 0)

    def test_summary_keeps_costs_broken_out(self):
        trade = self._run_to_completion(
            [open_from_signal(signal(), config(), state(), T0, 102.0).target_price])

        class Row:
            net_pnl = trade.net_pnl
            gross_pnl = trade.gross_pnl
            trading_fees = trade.fees_paid - trade.funding_paid
            funding_paid = trade.funding_paid
            exit_reason = trade.reason.value

        s = summarise([Row()], 3100.0, config())
        self.assertEqual(s["trades"], 1)
        self.assertEqual(s["wins"], 1)
        self.assertAlmostEqual(s["gross_pnl"], round(trade.gross_pnl, 2), places=2)
        self.assertGreater(s["trading_fees"], 0)
        self.assertIsNotNone(s["costs_as_pct_of_gross"])

    def test_empty_scorecard_does_not_divide_by_zero(self):
        s = summarise([], 3000.0, config())
        self.assertEqual(s["trades"], 0)
        self.assertEqual(s["win_rate_pct"], 0.0)
        self.assertIsNone(s["costs_as_pct_of_gross"])
        self.assertIsNone(s["realised_reward_risk"])


class TestPersistenceSurvivesRestart(unittest.IsolatedAsyncioTestCase):
    """
    The scheduler reloads state from the database every tick, because a free
    host redeploys often and open positions must not be silently abandoned.
    """

    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        import storage.models  # noqa: F401  — registers the tables
        from storage.database import Base
        from storage.repository import Repository

        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.Repository = Repository

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _repo(self):
        return self.Repository(self.maker())

    async def test_a_cycle_round_trips(self):
        repo = await self._repo()
        cycle = await repo.start_cycle(3000.0, 20000.0, 10.0, 0.20, 1.0, 0.70, False, True)
        self.assertEqual(cycle.status, "running")

        again = await (await self._repo()).get_running_cycle()
        self.assertIsNotNone(again)
        self.assertEqual(again.id, cycle.id)
        self.assertEqual(again.wallet, 3000.0)
        # The configuration travels with the row, so an old cycle still knows
        # what it was run under.
        self.assertEqual(again.leverage, 10.0)
        self.assertEqual(again.min_confidence, 0.70)

    async def test_an_open_position_survives_a_reload(self):
        repo = await self._repo()
        cycle = await repo.start_cycle(3000.0, 20000.0, 10.0, 0.20, 1.0, 0.70, True, True)
        pos = open_from_signal(signal(), config(), state(), T0, 102.0)
        row = await repo.save_position(cycle.id, pos)

        rows = await (await self._repo()).get_open_positions(cycle.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].symbol, "ethusdt")
        self.assertAlmostEqual(rows[0].coin_qty, pos.coin_qty, places=9)
        self.assertAlmostEqual(rows[0].entry_price, pos.entry_price, places=6)
        self.assertEqual(rows[0].id, row.id)

    async def test_trail_movement_is_persisted(self):
        repo = await self._repo()
        cycle = await repo.start_cycle(3000.0, 20000.0, 10.0, 0.20, 1.0, 0.70, True, True)
        cfg = config(trailing=TrailingStop(enabled=True, activate_at_r=0.5))
        pos = open_from_signal(signal(), cfg, state(), T0, 102.0)
        row = await repo.save_position(cycle.id, pos)

        resolve_at_price(pos, pos.entry_price * 1.015, T0 + timedelta(minutes=1), cfg, 3000.0)
        await repo.sync_position(row.id, pos)

        reloaded = (await (await self._repo()).get_open_positions(cycle.id))[0]
        self.assertAlmostEqual(reloaded.stop_price, pos.stop_price, places=6)
        self.assertTrue(reloaded.trail_active)

    async def test_closing_moves_a_position_into_the_trade_log(self):
        repo = await self._repo()
        cycle = await repo.start_cycle(3000.0, 20000.0, 10.0, 0.20, 1.0, 0.70, False, True)
        cfg = config()
        pos = open_from_signal(signal(), cfg, state(), T0, 102.0)
        row = await repo.save_position(cycle.id, pos)

        trade = resolve_at_price(pos, pos.target_price, T0 + timedelta(minutes=5), cfg, 2000.0)
        await repo.record_trade(cycle.id, trade)
        await repo.delete_position(row.id)

        fresh = await self._repo()
        self.assertEqual(await fresh.get_open_positions(cycle.id), [])
        trades = await fresh.get_cycle_trades(cycle.id)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].exit_reason, "target")
        self.assertGreater(trades[0].net_pnl, 0)
        # Costs stored separately, never netted away.
        self.assertGreater(trades[0].trading_fees, 0)
        self.assertAlmostEqual(
            trades[0].gross_pnl - trades[0].trading_fees - trades[0].funding_paid,
            trades[0].net_pnl, places=4,
        )

    async def test_ending_a_cycle_frees_the_running_slot(self):
        repo = await self._repo()
        cycle = await repo.start_cycle(3000.0, 20000.0, 10.0, 0.20, 1.0, 0.70, False, True)
        await repo.end_cycle(cycle.id, "hit_target", "reached 20k")

        fresh = await self._repo()
        self.assertIsNone(await fresh.get_running_cycle())
        recent = await fresh.get_recent_cycles()
        self.assertEqual(recent[0].status, "hit_target")
        self.assertIsNotNone(recent[0].ended_at)

    async def test_peak_wallet_only_ever_rises(self):
        repo = await self._repo()
        cycle = await repo.start_cycle(3000.0, 20000.0, 10.0, 0.20, 1.0, 0.70, False, True)
        for w in (3500.0, 3200.0, 4100.0, 2900.0):
            await repo.update_cycle_wallet(cycle.id, w)
        row = await (await self._repo()).get_running_cycle()
        self.assertEqual(row.wallet, 2900.0)
        self.assertEqual(row.peak_wallet, 4100.0)


class TestEndToEndCycle(unittest.IsolatedAsyncioTestCase):
    """
    Drive many ticks through the real persistence layer and check the invariant
    that matters most: money is conserved. Wallet plus committed margin plus
    realised P&L has to equal what we started with, or the simulator is lying.
    """

    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        import storage.models  # noqa: F401
        from storage.database import Base
        from storage.repository import Repository

        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.Repository = Repository

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _drive(self, prices, cfg=None):
        """Run the open/resolve loop the way the scheduler job does."""
        cfg = cfg or config()
        repo = self.Repository(self.maker())
        cycle = await repo.start_cycle(3000.0, 20000.0, 10.0, 0.20, 1.0, 0.70, False, True)
        wallet = 3000.0
        live, ids, realised = [], {}, 0.0

        for i, px in enumerate(prices):
            now = T0 + timedelta(minutes=i)
            survivors, new_ids = [], {}
            for idx, pos in enumerate(live):
                trade = resolve_at_price(pos, px, now, cfg, wallet)
                if trade is None:
                    new_ids[len(survivors)] = ids[idx]
                    survivors.append(pos)
                    continue
                wallet = trade.wallet_after
                realised += trade.net_pnl
                await repo.record_trade(cycle.id, trade)
                await repo.delete_position(ids[idx])
            live, ids = survivors, new_ids

            st = CycleState(cycle.id, wallet, wallet, live, ids)
            sig = signal(price=px, confidence=0.80)
            ok, _ = should_open(sig, cfg, st, now)
            if ok:
                pos = open_from_signal(sig, cfg, st, now, 102.0)
                if pos:
                    wallet -= pos.margin
                    row = await repo.save_position(cycle.id, pos)
                    ids[len(live)] = row.id
                    live.append(pos)
            await repo.update_cycle_wallet(cycle.id, wallet)

        return cycle, wallet, live, realised, repo

    async def test_money_is_conserved_across_a_long_run(self):
        prices = [1906.5 * (1 + 0.004 * ((i % 11) - 5)) for i in range(120)]
        cycle, wallet, live, realised, repo = await self._drive(prices)
        committed = sum(p.margin for p in live)
        self.assertAlmostEqual(wallet + committed, 3000.0 + realised, places=4)

    async def test_trades_are_all_recorded(self):
        prices = [1906.5 * (1 + 0.004 * ((i % 11) - 5)) for i in range(120)]
        cycle, wallet, live, realised, repo = await self._drive(prices)
        trades = await repo.get_cycle_trades(cycle.id)
        self.assertGreater(len(trades), 0)
        self.assertAlmostEqual(sum(t.net_pnl for t in trades), realised, places=4)

    async def test_the_wallet_never_goes_negative(self):
        """A stop bounds each loss, so the wallet cannot be driven below zero."""
        prices = [1906.5 * (1 - 0.002 * i) for i in range(90)]   # relentless decline
        _, wallet, live, _, _ = await self._drive(prices)
        self.assertGreaterEqual(wallet, 0.0)
        self.assertGreaterEqual(wallet + sum(p.margin for p in live), 0.0)

    async def test_a_cycle_that_hits_target_is_detected(self):
        cfg = config(target_wallet=3100.0)
        prices = [1906.5 * (1 + 0.004 * ((i % 11) - 5)) for i in range(120)]
        cycle, wallet, live, _, repo = await self._drive(prices, cfg)
        equity = wallet + sum(p.margin for p in live)
        if equity >= cfg.target_wallet:
            self.assertEqual(cycle_outcome(equity, cfg, len(live)), "hit_target")

    async def test_summary_reconciles_with_the_stored_trades(self):
        prices = [1906.5 * (1 + 0.004 * ((i % 11) - 5)) for i in range(120)]
        cycle, wallet, live, _, repo = await self._drive(prices)
        trades = await repo.get_cycle_trades(cycle.id)
        s = summarise(trades, wallet, config())
        self.assertEqual(s["trades"], len(trades))
        self.assertAlmostEqual(s["net_pnl"], round(sum(t.net_pnl for t in trades), 2), places=1)
        self.assertAlmostEqual(
            s["gross_pnl"] - s["trading_fees"] - s["funding_paid"], s["net_pnl"], places=1)


class TestDashboardSurface(unittest.TestCase):
    """The tab and its endpoint exist and are wired to each other."""

    def setUp(self):
        import scheduler.health as health
        self.html = health._HTML
        self.health = health

    def test_the_tab_and_its_loader_are_present(self):
        self.assertIn('id="tab-paper"', self.html)
        self.assertIn("async function loadPaper()", self.html)
        self.assertIn("switchTab('paper')", self.html)

    def test_switching_to_the_tab_loads_it(self):
        """The sidebar rewrite reindented this; the wiring is what matters."""
        self.assertIn("if(tab==='paper')", self.html)
        self.assertIn("loadPaper();", self.html)

    def test_the_endpoint_is_registered(self):
        import inspect
        src = inspect.getsource(self.health.setup_routes) \
            if hasattr(self.health, "setup_routes") else ""
        source = src or inspect.getsource(self.health)
        self.assertIn('"/api/paper"', source)

    def test_costs_are_shown_next_to_gross_not_netted_away(self):
        """The whole point of the scorecard — a net-only view hides fee drag."""
        self.assertIn("Gross P&amp;L", self.html)
        self.assertIn("Trading fees", self.html)
        self.assertIn("costs_as_pct_of_gross", self.html)

    def test_the_page_says_plainly_that_nothing_is_real(self):
        self.assertIn("never places a real order", self.html)


def _dashboard_html():
    from scheduler.health import _HTML
    return _HTML

class TestRealisedPnLExcludesLockedMargin(unittest.TestCase):
    """
    Margin in an open position has left the wallet but has not been lost.

    Captured 22 Sep 21:15 from the live desk: wallet 2194.11, one position
    holding 554.35 of margin, unrealised -11.22, start 3000. The page read
    realised P&L as wallet-minus-start and showed -805.89. The cycle was
    actually down 251.54 — the difference is the locked margin exactly, and
    the error grows with every position left open.
    """

    WALLET, MARGIN, UNREALISED, START = 2194.11, 554.35, -11.22, 3000.0

    def test_the_wrong_formula_reproduces_the_screenshot(self):
        self.assertAlmostEqual(self.WALLET - self.START, -805.89, places=2)

    def test_locked_margin_is_not_a_loss(self):
        realised = self.WALLET + self.MARGIN - self.START
        self.assertAlmostEqual(realised, -251.54, places=2)

    def test_equity_reconciles_with_the_corrected_figure(self):
        """Equity must equal start + realised + unrealised, or something lies."""
        equity = self.WALLET + self.MARGIN + self.UNREALISED
        realised = self.WALLET + self.MARGIN - self.START
        self.assertAlmostEqual(equity, self.START + realised + self.UNREALISED, places=2)

    def test_the_page_uses_the_corrected_formula(self):
        html = _dashboard_html()
        self.assertIn("c.wallet + margin - c.starting_wallet", html)
        self.assertNotIn("const realised = c.wallet - c.starting_wallet", html)
