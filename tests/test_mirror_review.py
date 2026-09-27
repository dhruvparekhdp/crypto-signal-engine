"""
Mirror review (27 Sep): opposite-direction candidate generation, round-0
confirmation-gated opening, the live re-review loop's gating and 3-call
cap, the higher-confidence tie-break, REJECT-at-any-round, timeout, and
the mirror_review_enabled=False regression (today's behaviour, byte for
byte).

Follows the idiom in tests/test_signal_skip_reason.py: a fresh in-memory
SQLite DB per test, AsyncSessionFactory patched to it, storage modules
imported inside test bodies so the ambient DATABASE_URL from other test
modules can never leak in.
"""
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from analysis.crypto_signal import CryptoSignal, make_mirror_signal
from analysis.crypto_state import CryptoState
from analysis.mirror_review import (
    TrackedCandidate,
    TrackedPair,
    elapsed_pct,
    timeframe_minutes,
)
from scheduler.runner import AppRunner
from storage.database import Base


async def _fresh_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _signal(**over):
    base = dict(
        symbol="btcusdt", signal_type="confluence", direction="long",
        trigger_description="Confluence long: RSI + volume", confidence=0.55,
        current_price=60000.0, target_price=61200.0, stop_loss=59400.0,
        edge_pct=2.0, stake_pct=0.015, timeframe="1h", sentiment_score=0.0,
        indicators_summary="", timestamp=datetime.now(UTC))
    base.update(over)
    return CryptoSignal(**base)


def _unavailable_groq():
    """A GroqSentinel double that never fires (is_available False) — the
    AI review step returns ("", "") and confidence is left exactly at its
    seed, which is what most of these tests want."""
    g = MagicMock()
    g.is_available = False
    g.postmortem_available = False
    return g


def _mock_groq(verdict="APPROVE", delta=0.0, summary="looks fine"):
    g = MagicMock()
    g.is_available = True
    g.postmortem_available = False
    g.last_factors = ""
    g.last_model = "mock-model"
    g.last_latency_ms = 5
    g.review_signal_candidate = AsyncMock(return_value=(delta, summary, verdict))
    return g


# ── 1. Mirror candidate generation ──────────────────────────────────────────

class TestMakeMirrorSignal(unittest.TestCase):
    def test_opposite_direction_and_role(self):
        sig = _signal(direction="long")
        mirror = make_mirror_signal(sig)
        self.assertEqual(mirror.direction, "short")
        self.assertEqual(mirror.candidate_role, "mirror")
        self.assertEqual(sig.candidate_role, "primary")

    def test_opposite_direction_short_to_long(self):
        sig = _signal(direction="short", target_price=58800.0, stop_loss=60600.0)
        mirror = make_mirror_signal(sig)
        self.assertEqual(mirror.direction, "long")

    def test_same_symbol_and_entry_price(self):
        sig = _signal()
        mirror = make_mirror_signal(sig)
        self.assertEqual(mirror.symbol, sig.symbol)
        self.assertEqual(mirror.current_price, sig.current_price)

    def test_confidence_seeded_at_originals_pre_review_confidence(self):
        sig = _signal(confidence=0.63)
        mirror = make_mirror_signal(sig)
        self.assertEqual(mirror.confidence, 0.63)

    def test_target_and_stop_are_not_an_exact_mechanical_mirror(self):
        """Jittered independently — across many draws, at least some must
        differ from the mechanical (no-jitter) mirror numbers, proving the
        jitter is actually applied and not a no-op."""
        sig = _signal(current_price=60000.0, target_price=61200.0, stop_loss=59400.0)
        mechanical_target_pct = (61200.0 - 60000.0) / 60000.0
        mechanical_stop_pct = (60000.0 - 59400.0) / 60000.0

        saw_a_difference = False
        for _ in range(30):
            mirror = make_mirror_signal(sig)
            m_target_pct = abs(mirror.target_price - mirror.current_price) / mirror.current_price
            m_stop_pct = abs(mirror.current_price - mirror.stop_loss) / mirror.current_price
            if (abs(m_target_pct - mechanical_target_pct) > 1e-9
                    or abs(m_stop_pct - mechanical_stop_pct) > 1e-9):
                saw_a_difference = True
            # Similar magnitude: within the configured jitter range (+/-20%
            # default, generous margin for float rounding).
            self.assertLess(abs(m_target_pct - mechanical_target_pct),
                            mechanical_target_pct * 0.35 + 1e-6)
            self.assertLess(abs(m_stop_pct - mechanical_stop_pct),
                            mechanical_stop_pct * 0.35 + 1e-6)
        self.assertTrue(saw_a_difference, "mirror levels were identical to the mechanical "
                                          "reflection on every one of 30 draws")

    def test_edge_and_stake_recomputed_from_mirrors_own_numbers(self):
        sig = _signal(edge_pct=99.0, stake_pct=0.5)  # deliberately wrong, must not be copied
        mirror = make_mirror_signal(sig)
        self.assertNotEqual(mirror.edge_pct, sig.edge_pct)
        self.assertNotEqual(mirror.stake_pct, sig.stake_pct)

    def test_applies_regardless_of_signal_type(self):
        for stype in ("rsi_divergence", "volume_spike", "sentiment_shift",
                      "bollinger_squeeze", "trend_continuation", "confluence",
                      "v2_setup_b"):
            sig = _signal(signal_type=stype)
            mirror = make_mirror_signal(sig)
            self.assertEqual(mirror.signal_type, stype)   # unchanged, per spec
            self.assertEqual(mirror.direction, "short")

    def test_labels_are_never_bare_primary_or_mirror(self):
        """The UI rule: always paired with direction. Exercised here at the
        data level — candidate_role is the raw value the UI must pair with
        direction, never displayed alone."""
        sig = _signal(direction="long")
        mirror = make_mirror_signal(sig)
        label_primary = f"{sig.candidate_role.capitalize()} ({sig.direction.capitalize()})"
        label_mirror = f"{mirror.candidate_role.capitalize()} ({mirror.direction.capitalize()})"
        self.assertEqual(label_primary, "Primary (Long)")
        self.assertEqual(label_mirror, "Mirror (Short)")


# ── 2. Pure helper functions ────────────────────────────────────────────────

class TestTimeframeAndElapsed(unittest.TestCase):
    def test_known_timeframes(self):
        self.assertEqual(timeframe_minutes("30m"), 30.0)
        self.assertEqual(timeframe_minutes("1h"), 60.0)
        self.assertEqual(timeframe_minutes("4h"), 240.0)
        self.assertEqual(timeframe_minutes("1d"), 1440.0)

    def test_elapsed_pct_grows_with_time(self):
        sig = _signal(timeframe="1h", timestamp=datetime.now(UTC) - timedelta(minutes=30))
        pct = elapsed_pct(sig, datetime.now(UTC))
        self.assertAlmostEqual(pct, 0.5, delta=0.02)

    def test_elapsed_pct_past_timeframe_exceeds_one(self):
        sig = _signal(timeframe="30m", timestamp=datetime.now(UTC) - timedelta(minutes=45))
        pct = elapsed_pct(sig, datetime.now(UTC))
        self.assertGreaterEqual(pct, 1.0)


# ── 3. Round-0: confirmation-gated opening ──────────────────────────────────

def _runner_with_state(price=60000.0):
    runner = AppRunner()
    runner.notifier = AsyncMock()
    st = CryptoState(symbol="btcusdt", base_asset="BTC")
    st.current_price = price
    runner.crypto_store._states["btcusdt"] = st
    return runner, st


@pytest.mark.asyncio
async def test_mirror_review_disabled_reproduces_todays_exact_behaviour():
    """Regression: with mirror_review_enabled False, a fired signal takes
    the original single-candidate path — _handle_mirror_candidates is never
    called, no mirror is ever generated, and the log carries exactly one
    row for the fired signal, exactly like before this feature existed."""
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state()
    runner.groq_sentinel = _unavailable_groq()

    fired = _signal(confidence=0.80)
    runner.crypto_engine.process = MagicMock(return_value=[fired])
    runner.crypto_engine.forget = MagicMock()

    with patch("scheduler.runner.settings.mirror_review_enabled", False), \
         patch("scheduler.runner.settings.crypto_min_confidence", 0.0), \
         patch("scheduler.runner.settings.paper_trading_enabled", True), \
         patch("scheduler.runner.AppRunner._handle_mirror_candidates",
               new=AsyncMock(side_effect=AssertionError("must not be called"))), \
         patch("scheduler.runner.AsyncSessionFactory", sm):
        from storage.repository import Repository
        async with sm() as s:
            repo = Repository(s)
            scfg = await repo.get_strategy_config()
            pcfg = await repo.get_paper_config()

        await runner._analyse_states([st], scfg, pcfg)

        async with sm() as s:
            rows = await Repository(s).get_recent_crypto_signals(hours=24)

    assert len(rows) == 1
    assert rows[0].candidate_role == "primary"
    assert rows[0].mirror_of_log_id == 0
    assert runner._tracked_signal_pairs == {}
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_strong_signal_opens_immediately_at_round_zero():
    """Confidence already at/above pcfg.min_confidence and no REJECT verdict
    -> opens using round-0 values, exactly like today's single-signal path,
    just with a mirror also reviewed and (here) losing the tie."""
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state()
    runner.groq_sentinel = _unavailable_groq()   # confidence stays at seed
    with patch("scheduler.runner.AsyncSessionFactory", sm):
        from storage.repository import Repository
        async with sm() as s:
            repo = Repository(s)
            await repo.update_paper_config(min_confidence=0.70)
            scfg = await repo.get_strategy_config()
            pcfg = await repo.get_paper_config()

        sig = _signal(confidence=0.80, direction="long")
        await runner._handle_mirror_candidates(sig, st, [st], scfg, pcfg)

        assert len(runner._pending_paper_signals) == 1
        winner_sig, winner_state = runner._pending_paper_signals[0]
        assert winner_state is st

        async with sm() as s:
            rows = await Repository(s).get_recent_crypto_signals(hours=24)
    assert len(rows) == 2   # primary round-0 + mirror round-0
    await engine.dispose()


@pytest.mark.asyncio
async def test_tie_break_higher_confidence_wins_loser_marked_rejected():
    """Both primary and mirror seed at the same confidence (Groq
    unavailable, so nothing nudges either) and both clear pcfg's floor at
    round 0 -> exactly one wins, the other is rejected with the documented
    reason. This is the corrected design decision, verified explicitly."""
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state()
    runner.groq_sentinel = _unavailable_groq()
    with patch("scheduler.runner.AsyncSessionFactory", sm):
        from storage.repository import Repository
        async with sm() as s:
            repo = Repository(s)
            await repo.update_paper_config(min_confidence=0.60)
            scfg = await repo.get_strategy_config()
            pcfg = await repo.get_paper_config()

        sig = _signal(confidence=0.90, direction="long")
        await runner._handle_mirror_candidates(sig, st, [st], scfg, pcfg)

        # Exactly one candidate queued to open — never both.
        assert len(runner._pending_paper_signals) == 1

        async with sm() as s:
            rows = await Repository(s).get_recent_crypto_signals(hours=24)
    assert len(rows) == 2
    rejected = [r for r in rows if r.rejection_reason]
    traded = [r for r in rows if not r.rejection_reason]
    assert len(rejected) == 1
    assert len(traded) == 1
    assert rejected[0].rejection_reason == "opposite_side_won"
    # The two candidates point opposite directions.
    assert rejected[0].direction != traded[0].direction
    await engine.dispose()


@pytest.mark.asyncio
async def test_reject_verdict_stops_tracking_and_records_the_reason():
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state()
    runner.groq_sentinel = _mock_groq(verdict="REJECT", summary="structurally broken")
    with patch("scheduler.runner.AsyncSessionFactory", sm):
        from storage.repository import Repository
        async with sm() as s:
            repo = Repository(s)
            await repo.update_paper_config(min_confidence=0.95)  # nothing opens immediately
            scfg = await repo.get_strategy_config()
            pcfg = await repo.get_paper_config()

        sig = _signal(confidence=0.55, direction="long")
        await runner._handle_mirror_candidates(sig, st, [st], scfg, pcfg)

        assert runner._pending_paper_signals == []
        assert runner._tracked_signal_pairs == {}   # both REJECTed at round 0

        async with sm() as s:
            rows = await Repository(s).get_recent_crypto_signals(hours=24)
    assert len(rows) == 2
    assert all(r.rejection_reason == "structurally broken" for r in rows)
    await engine.dispose()


@pytest.mark.asyncio
async def test_below_floor_candidates_are_tracked_not_opened_or_rejected():
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state()
    runner.groq_sentinel = _unavailable_groq()
    with patch("scheduler.runner.AsyncSessionFactory", sm):
        from storage.repository import Repository
        async with sm() as s:
            repo = Repository(s)
            await repo.update_paper_config(min_confidence=0.90)
            scfg = await repo.get_strategy_config()
            pcfg = await repo.get_paper_config()

        sig = _signal(confidence=0.55, direction="long")
        await runner._handle_mirror_candidates(sig, st, [st], scfg, pcfg)

        assert runner._pending_paper_signals == []
        assert len(runner._tracked_signal_pairs) == 1
        pair = next(iter(runner._tracked_signal_pairs.values()))
        assert pair.primary.state == "tracking"
        assert pair.mirror.state == "tracking"
    await engine.dispose()


# ── 4. The live re-review loop ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reviews_are_gated_by_elapsed_and_confidence_move_and_capped_at_three_calls():
    """A candidate with a huge confidence swing but not enough elapsed time
    is left alone; once enough time has passed it is reviewed, and once
    review_round reaches mirror_review_max_rounds no further AI calls are
    made even though every other condition still holds."""
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state(price=60000.0)
    groq = _mock_groq(verdict="APPROVE", delta=0.0, summary="still fine")
    runner.groq_sentinel = groq
    runner.crypto_store._states["btcusdt"] = st

    with patch("scheduler.runner.AsyncSessionFactory", sm), \
         patch("scheduler.runner.settings.mirror_review_enabled", True), \
         patch("scheduler.runner.settings.mirror_review_min_elapsed_pct", 0.33), \
         patch("scheduler.runner.settings.mirror_review_confidence_delta_threshold", 0.05), \
         patch("scheduler.runner.settings.mirror_review_max_rounds", 2), \
         patch("scheduler.runner.local_confidence_estimate",
               side_effect=[0.95, 0.95, 0.60, 0.60, 0.95, 0.95]):
        from storage.repository import Repository
        async with sm() as s:
            repo = Repository(s)
            await repo.update_paper_config(min_confidence=0.99)  # never wins outright here
            log_id = await repo.log_crypto_signal(
                symbol="btcusdt", signal_type="confluence", direction="long",
                trigger_description="t", confidence=0.55, current_price=60000.0,
                target_price=61200.0, stop_loss=59400.0, edge_pct=2.0,
                stake_pct=0.015, timeframe="1h")

        sig = _signal(confidence=0.55, timeframe="1h",
                      timestamp=datetime.now(UTC) - timedelta(minutes=5))  # elapsed_pct ~ 0.08
        cand = TrackedCandidate(signal=sig, log_id=log_id, root_log_id=log_id,
                                last_ai_review_at=datetime.now(UTC),
                                last_reviewed_confidence=0.55)
        pair = TrackedPair(primary=cand)
        runner._tracked_signal_pairs[log_id] = pair

        # Not enough time elapsed yet -> no AI call.
        await runner._mirror_review_job()
        assert groq.review_signal_candidate.call_count == 0
        assert cand.review_round == 0

        # Push it past the elapsed threshold -> should now fire.
        cand.signal.timestamp = datetime.now(UTC) - timedelta(minutes=25)  # ~0.42 of 1h
        await runner._mirror_review_job()
        assert groq.review_signal_candidate.call_count == 1
        assert cand.review_round == 1

        # A second re-review, still moved, still under the cap.
        cand.signal.timestamp = datetime.now(UTC) - timedelta(minutes=45)
        await runner._mirror_review_job()
        assert groq.review_signal_candidate.call_count == 2
        assert cand.review_round == 2

        # Cap reached (max_rounds=2) — a third would exceed 1 + 2 = 3 total
        # calls. No further AI call, ever, however long it keeps moving.
        cand.state = "tracking"
        cand.signal.timestamp = datetime.now(UTC) - timedelta(minutes=50)
        await runner._mirror_review_job()
        assert groq.review_signal_candidate.call_count == 2
        assert cand.review_round == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_reject_during_live_re_review_stops_tracking():
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state(price=60000.0)
    groq = _mock_groq(verdict="REJECT", summary="no longer holds")
    runner.groq_sentinel = groq

    with patch("scheduler.runner.AsyncSessionFactory", sm), \
         patch("scheduler.runner.settings.mirror_review_enabled", True), \
         patch("scheduler.runner.settings.mirror_review_min_elapsed_pct", 0.1), \
         patch("scheduler.runner.settings.mirror_review_confidence_delta_threshold", 0.01), \
         patch("scheduler.runner.local_confidence_estimate", return_value=0.80):
        from storage.repository import Repository
        async with sm() as s:
            repo = Repository(s)
            await repo.update_paper_config(min_confidence=0.99)
            log_id = await repo.log_crypto_signal(
                symbol="btcusdt", signal_type="confluence", direction="long",
                trigger_description="t", confidence=0.55, current_price=60000.0,
                target_price=61200.0, stop_loss=59400.0, edge_pct=2.0,
                stake_pct=0.015, timeframe="1h")

        sig = _signal(confidence=0.55, timeframe="1h",
                      timestamp=datetime.now(UTC) - timedelta(minutes=25))
        cand = TrackedCandidate(signal=sig, log_id=log_id, root_log_id=log_id,
                                last_ai_review_at=datetime.now(UTC),
                                last_reviewed_confidence=0.55)
        runner._tracked_signal_pairs[log_id] = TrackedPair(primary=cand)

        await runner._mirror_review_job()

        assert cand.state == "rejected"
        assert cand.rejection_reason == "no longer holds"
        assert runner._tracked_signal_pairs == {}   # pair fully settled, popped
    await engine.dispose()


@pytest.mark.asyncio
async def test_timeout_without_confirmation_marks_rejected_no_more_ai_calls():
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state(price=60000.0)
    groq = _mock_groq(verdict="APPROVE", delta=0.0)
    runner.groq_sentinel = groq

    with patch("scheduler.runner.AsyncSessionFactory", sm), \
         patch("scheduler.runner.settings.mirror_review_enabled", True):
        from storage.repository import Repository
        async with sm() as s:
            repo = Repository(s)
            await repo.update_paper_config(min_confidence=0.99)
            log_id = await repo.log_crypto_signal(
                symbol="btcusdt", signal_type="confluence", direction="long",
                trigger_description="t", confidence=0.55, current_price=60000.0,
                target_price=61200.0, stop_loss=59400.0, edge_pct=2.0,
                stake_pct=0.015, timeframe="30m")

        # Timeframe fully elapsed (30m signal, fired 40 minutes ago).
        sig = _signal(confidence=0.55, timeframe="30m",
                      timestamp=datetime.now(UTC) - timedelta(minutes=40))
        cand = TrackedCandidate(signal=sig, log_id=log_id, root_log_id=log_id,
                                last_ai_review_at=datetime.now(UTC),
                                last_reviewed_confidence=0.55)
        runner._tracked_signal_pairs[log_id] = TrackedPair(primary=cand)

        await runner._mirror_review_job()

        assert cand.state == "rejected"
        assert cand.rejection_reason == "ai_review_timed_out"
        assert groq.review_signal_candidate.call_count == 0   # no AI call spent on it
        assert runner._tracked_signal_pairs == {}
    await engine.dispose()


@pytest.mark.asyncio
async def test_live_loop_win_queues_the_paper_trade_and_stops_the_opposite_side():
    engine, sm = await _fresh_db()
    runner, st = _runner_with_state(price=60000.0)
    groq = _mock_groq(verdict="APPROVE", delta=0.0)
    runner.groq_sentinel = groq

    with patch("scheduler.runner.AsyncSessionFactory", sm), \
         patch("scheduler.runner.settings.mirror_review_enabled", True), \
         patch("scheduler.runner.settings.mirror_review_min_elapsed_pct", 0.1), \
         patch("scheduler.runner.settings.mirror_review_confidence_delta_threshold", 0.01), \
         patch("scheduler.runner.local_confidence_estimate", return_value=0.95):
        from storage.repository import Repository
        async with sm() as s:
            repo = Repository(s)
            await repo.update_paper_config(min_confidence=0.70)
            primary_log_id = await repo.log_crypto_signal(
                symbol="btcusdt", signal_type="confluence", direction="long",
                trigger_description="t", confidence=0.55, current_price=60000.0,
                target_price=61200.0, stop_loss=59400.0, edge_pct=2.0,
                stake_pct=0.015, timeframe="1h")
            mirror_log_id = await repo.log_crypto_signal(
                symbol="btcusdt", signal_type="confluence", direction="short",
                trigger_description="t", confidence=0.55, current_price=60000.0,
                target_price=58800.0, stop_loss=60600.0, edge_pct=2.0,
                stake_pct=0.015, timeframe="1h", candidate_role="mirror",
                mirror_of_log_id=primary_log_id)

        now = datetime.now(UTC) - timedelta(minutes=25)
        p_sig = _signal(confidence=0.55, timeframe="1h", timestamp=now, direction="long")
        m_sig = _signal(confidence=0.55, timeframe="1h", timestamp=now, direction="short",
                        target_price=58800.0, stop_loss=60600.0)
        p_cand = TrackedCandidate(signal=p_sig, log_id=primary_log_id, root_log_id=primary_log_id,
                                  last_ai_review_at=now, last_reviewed_confidence=0.55)
        m_cand = TrackedCandidate(signal=m_sig, log_id=mirror_log_id, root_log_id=mirror_log_id,
                                  last_ai_review_at=now, last_reviewed_confidence=0.55)
        runner._tracked_signal_pairs[primary_log_id] = TrackedPair(primary=p_cand, mirror=m_cand)

        await runner._mirror_review_job()

        assert len(runner._pending_paper_signals) == 1
        assert p_cand.state == "traded"
        assert m_cand.state == "rejected"
        assert m_cand.rejection_reason == "opposite_side_won"
        assert runner._tracked_signal_pairs == {}
    await engine.dispose()


if __name__ == "__main__":
    unittest.main()
