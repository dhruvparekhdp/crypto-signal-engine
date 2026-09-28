"""
Precedent-based event context: NOT a price forecaster. Real historical
precedent (AI), measured against OUR OWN Binance lake data (never the AI),
synthesised (AI, grounded in the measured numbers), and only ever applied
as a bounded, advisory nudge — same clamp discipline as calendar_caution.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from analysis.event_precedent import (
    allows_extended_hold,
    brief_from_row,
    confidence_bucket,
    confidence_penalty,
    current_brief,
    is_usable,
    measure_window,
    parse_precedents,
    parse_synthesis,
    set_active_briefs,
)
from collectors.binance_lake import lake_path


def _write_daily(root: Path, symbol: str, start: datetime, days: int,
                 base_price: float = 100.0, base_volume: float = 1000.0,
                 jump_at: int | None = None, jump_pct: float = 0.0,
                 volume_mult_from: int | None = None, volume_mult: float = 1.0) -> None:
    """A synthetic month of daily klines, written straight to the lake path
    measure_window() reads — no network, no zip, just the columns read()
    needs."""
    rows = []
    price = base_price
    for i in range(days):
        if jump_at is not None and i == jump_at:
            price *= (1 + jump_pct / 100.0)
        vol = base_volume
        if volume_mult_from is not None and i >= volume_mult_from:
            vol *= volume_mult
        ts = start + timedelta(days=i)
        rows.append({"ts": ts, "open": price, "high": price * 1.01, "low": price * 0.99,
                     "close": price, "volume": vol})
    by_month: dict[str, list[dict]] = {}
    for r in rows:
        by_month.setdefault(r["ts"].strftime("%Y-%m"), []).append(r)
    for month, month_rows in by_month.items():
        path = lake_path(root, "um", "klines", symbol, "1d", month)
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(month_rows).to_parquet(path, index=False)


class TestMeasureWindow(unittest.TestCase):
    def test_real_numbers_come_from_the_lake_not_the_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            start = datetime(2024, 1, 1)
            # 14 quiet days, then a +10% jump on day 14 that holds through
            # the event window (days 14-16), with volume doubling from then.
            _write_daily(root, "BTCUSDT", start, 20, base_price=100.0,
                        jump_at=14, jump_pct=10.0, volume_mult_from=14, volume_mult=2.0)
            m = measure_window("BTCUSDT", start + timedelta(days=14),
                               start + timedelta(days=16), root=str(root))
            self.assertIsNotNone(m)
            self.assertAlmostEqual(m["price_change_pct"], 0.0, delta=0.5)
            self.assertGreater(m["volume_change_pct"], 50.0)

    def test_no_coverage_returns_none_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Nothing written at all: predates the lake, or the coin didn't
            # exist yet. Must be a clean None, never an exception.
            got = measure_window("DOESNOTEXISTUSDT", datetime(2019, 1, 1),
                                 datetime(2019, 1, 3), root=tmp)
            self.assertIsNone(got)

    def test_too_little_baseline_history_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            start = datetime(2024, 1, 1)
            # Only 3 days total, none of it before the "event" — no trailing
            # baseline to compare volume against.
            _write_daily(root, "ETHUSDT", start, 3)
            got = measure_window("ETHUSDT", start, start + timedelta(days=1), root=str(root))
            self.assertIsNone(got)


class TestParsePrecedents(unittest.TestCase):
    def test_empty_list_is_a_valid_answer(self):
        """The model saying 'I found nothing real and comparable' must
        parse to an empty list cleanly, not be treated as a parse failure —
        this is the expected, correct outcome for a novel event."""
        self.assertEqual(parse_precedents({"precedents": []}), [])
        self.assertEqual(parse_precedents({}), [])

    def test_an_unparseable_date_is_dropped_not_guessed(self):
        got = parse_precedents({"precedents": [
            {"name": "real one", "start_date": "2018-12-19", "end_date": "2018-12-20",
             "confidence_this_is_real": "high"},
            {"name": "bad date", "start_date": "not-a-date", "confidence_this_is_real": "high"},
            {"name": "", "start_date": "2020-01-01", "confidence_this_is_real": "high"},
        ]})
        self.assertEqual([p["name"] for p in got], ["real one"])

    def test_unknown_confidence_defaults_to_low_not_high(self):
        got = parse_precedents({"precedents": [
            {"name": "x", "start_date": "2020-01-01", "confidence_this_is_real": "extremely sure"},
        ]})
        self.assertEqual(got[0]["confidence_this_is_real"], "low")

    def test_a_single_day_event_gets_an_end_date_equal_to_start(self):
        got = parse_precedents({"precedents": [
            {"name": "x", "start_date": "2020-01-01", "confidence_this_is_real": "medium"},
        ]})
        self.assertEqual(got[0]["end_date"], "2020-01-01")


class TestConfidenceBucket(unittest.TestCase):
    def test_worst_confidence_wins(self):
        precs = [{"confidence_this_is_real": "high"}, {"confidence_this_is_real": "low"}]
        self.assertEqual(confidence_bucket(precs), "low")
        precs = [{"confidence_this_is_real": "high"}, {"confidence_this_is_real": "medium"}]
        self.assertEqual(confidence_bucket(precs), "medium")
        precs = [{"confidence_this_is_real": "high"}]
        self.assertEqual(confidence_bucket(precs), "high")

    def test_no_precedents_used_is_low(self):
        self.assertEqual(confidence_bucket([]), "low")


class TestParseSynthesis(unittest.TestCase):
    def test_sample_size_is_ours_not_the_models(self):
        """The caller's own count of occurrences with real measured data,
        never whatever number the model happens to report."""
        got = parse_synthesis({"direction_bias": "bullish", "sample_size": 999}, 3)
        self.assertEqual(got["sample_size"], 3)

    def test_magnitude_and_duration_are_clamped(self):
        got = parse_synthesis({"typical_magnitude_pct": 9999, "typical_duration_days": -9999}, 1)
        self.assertLessEqual(got["typical_magnitude_pct"], 50.0)
        self.assertGreaterEqual(got["typical_duration_days"], -30.0)

    def test_unknown_bias_defaults_to_mixed(self):
        got = parse_synthesis({"direction_bias": "extremely bullish probably"}, 1)
        self.assertEqual(got["direction_bias"], "mixed")


class TestGates(unittest.TestCase):
    """These must never let a weak brief touch anything live, and never do
    more than the same penalty-bucket scale calendar_caution already uses."""

    def test_below_min_sample_size_is_not_usable(self):
        brief = {"sample_size": 1, "confidence_real": "high"}
        self.assertFalse(is_usable(brief, min_sample_size=2))
        self.assertTrue(is_usable(dict(brief, sample_size=2), min_sample_size=2))

    def test_low_confidence_is_never_usable_however_large_the_sample(self):
        brief = {"sample_size": 10, "confidence_real": "low"}
        self.assertFalse(is_usable(brief, min_sample_size=2))

    def test_penalty_matches_calendar_cautions_own_scale(self):
        # Identical to analysis.crypto_engine.process()'s calendar_caution
        # bucket on purpose — the instruction was to reuse the existing
        # scale, not invent a second one.
        self.assertEqual(confidence_penalty(5), 0.08)
        self.assertEqual(confidence_penalty(4), 0.06)
        self.assertEqual(confidence_penalty(3), 0.03)
        self.assertEqual(confidence_penalty(2), 0.01)

    def test_extended_hold_needs_direction_agreement_with_a_real_lean(self):
        bullish = {"direction_bias": "bullish"}
        self.assertTrue(allows_extended_hold(bullish, "long"))
        self.assertFalse(allows_extended_hold(bullish, "short"))
        bearish = {"direction_bias": "bearish"}
        self.assertTrue(allows_extended_hold(bearish, "short"))
        self.assertFalse(allows_extended_hold(bearish, "long"))

    def test_mixed_bias_grants_either_direction(self):
        mixed = {"direction_bias": "mixed"}
        self.assertTrue(allows_extended_hold(mixed, "long"))
        self.assertTrue(allows_extended_hold(mixed, "short"))


class TestActiveBriefCache(unittest.TestCase):
    def tearDown(self):
        set_active_briefs({})

    def test_current_brief_is_none_until_the_job_populates_it(self):
        self.assertIsNone(current_brief(datetime(2026, 10, 1)))

    def test_current_brief_only_matches_inside_its_own_window(self):
        set_active_briefs({"k": {"window_start": datetime(2026, 10, 1),
                                 "window_end": datetime(2026, 10, 5)}})
        self.assertIsNone(current_brief(datetime(2026, 9, 30)))
        self.assertIsNotNone(current_brief(datetime(2026, 10, 3)))
        self.assertIsNone(current_brief(datetime(2026, 10, 6)))

    def test_brief_from_row_windows_around_lookahead_and_a_short_tail(self):
        from types import SimpleNamespace
        row = SimpleNamespace(
            event_key="fomc:2026-10-28", event_name="FOMC rate decision",
            event_at=datetime(2026, 10, 28, 18, 0), level=5, sample_size=3,
            confidence_real="medium", direction_bias="bullish",
            typical_magnitude_pct=2.1, typical_duration_days=1.5, summary="s")
        brief = brief_from_row(row, lookahead_days=7)
        self.assertEqual(brief["window_start"], datetime(2026, 10, 21, 18, 0))
        self.assertEqual(brief["window_end"], datetime(2026, 10, 30, 18, 0))


class TestEngineAppliesPrecedent(unittest.TestCase):
    """
    The wiring in CryptoEngine.process() — same style as
    tests/test_calendar_caution.py's TestEngineAppliesCaution: one forced
    signal from confluence, everything else patched off, exercising the
    real code path rather than re-deriving the formula beside it.
    """

    def _engine_with_one_forced_signal(self, confidence=0.75, direction="long"):
        import analysis.crypto_engine as ce
        from analysis.crypto_signal import CryptoSignal

        engine = ce.CryptoEngine()
        sig = CryptoSignal(
            symbol="btcusdt", signal_type="confluence", direction=direction,
            trigger_description="t", confidence=confidence, current_price=100.0,
            target_price=101.0 if direction == "long" else 99.0,
            stop_loss=99.5 if direction == "long" else 100.5,
            edge_pct=1.0, stake_pct=0.01, timeframe="20m", sentiment_score=0.0,
            indicators_summary="", timestamp=datetime(2026, 1, 1))
        engine.confluence.analyze = lambda state: sig
        engine.rsi_divergence.analyze = lambda state: None
        engine.volume_spike.analyze = lambda state: None
        engine.bollinger_squeeze.analyze = lambda state: None
        engine.sentiment_shift.analyze = lambda state: None
        return engine, sig

    def _state(self):
        from analysis.crypto_state import CryptoState
        return CryptoState(symbol="btcusdt", base_asset="BTC", current_price=100.0)

    def tearDown(self):
        set_active_briefs({})

    def test_a_usable_brief_trims_confidence_by_its_level_bucket(self):
        from unittest.mock import patch

        import analysis.crypto_engine as ce

        set_active_briefs({"k": {"window_start": datetime(2025, 1, 1),
                                 "window_end": datetime(2027, 1, 1),
                                 "event_name": "FOMC rate decision", "level": 5,
                                 "sample_size": 3, "confidence_real": "medium",
                                 "direction_bias": "mixed"}})
        engine, sig = self._engine_with_one_forced_signal(confidence=0.75)
        with patch.object(ce.settings, "orderflow_enabled", False), \
             patch.object(ce.settings, "calendar_caution_enabled", False), \
             patch.object(ce.settings, "crypto_htf_filter_enabled", False), \
             patch.object(ce.settings, "event_precedent_enabled", True), \
             patch.object(ce.settings, "event_precedent_min_sample_size", 2):
            fired = engine.process(self._state())
        self.assertEqual(len(fired), 1)
        self.assertAlmostEqual(fired[0].confidence, 0.75 - 0.08, places=4)

    def test_a_brief_below_the_min_sample_size_does_nothing(self):
        from unittest.mock import patch

        import analysis.crypto_engine as ce

        set_active_briefs({"k": {"window_start": datetime(2025, 1, 1),
                                 "window_end": datetime(2027, 1, 1),
                                 "event_name": "FOMC rate decision", "level": 5,
                                 "sample_size": 1, "confidence_real": "high",
                                 "direction_bias": "mixed"}})
        engine, sig = self._engine_with_one_forced_signal(confidence=0.75)
        with patch.object(ce.settings, "orderflow_enabled", False), \
             patch.object(ce.settings, "calendar_caution_enabled", False), \
             patch.object(ce.settings, "crypto_htf_filter_enabled", False), \
             patch.object(ce.settings, "event_precedent_enabled", True), \
             patch.object(ce.settings, "event_precedent_min_sample_size", 2):
            fired = engine.process(self._state())
        self.assertEqual(fired[0].confidence, 0.75)

    def test_a_low_confidence_real_brief_does_nothing_however_large_the_sample(self):
        from unittest.mock import patch

        import analysis.crypto_engine as ce

        set_active_briefs({"k": {"window_start": datetime(2025, 1, 1),
                                 "window_end": datetime(2027, 1, 1),
                                 "event_name": "FOMC rate decision", "level": 5,
                                 "sample_size": 10, "confidence_real": "low",
                                 "direction_bias": "mixed"}})
        engine, sig = self._engine_with_one_forced_signal(confidence=0.75)
        with patch.object(ce.settings, "orderflow_enabled", False), \
             patch.object(ce.settings, "calendar_caution_enabled", False), \
             patch.object(ce.settings, "crypto_htf_filter_enabled", False), \
             patch.object(ce.settings, "event_precedent_enabled", True), \
             patch.object(ce.settings, "event_precedent_min_sample_size", 2):
            fired = engine.process(self._state())
        self.assertEqual(fired[0].confidence, 0.75)

    def test_disabling_the_setting_skips_it_entirely_even_with_an_active_brief(self):
        from unittest.mock import patch

        import analysis.crypto_engine as ce

        set_active_briefs({"k": {"window_start": datetime(2025, 1, 1),
                                 "window_end": datetime(2027, 1, 1),
                                 "event_name": "FOMC rate decision", "level": 5,
                                 "sample_size": 5, "confidence_real": "high",
                                 "direction_bias": "mixed"}})
        engine, sig = self._engine_with_one_forced_signal(confidence=0.75)
        with patch.object(ce.settings, "orderflow_enabled", False), \
             patch.object(ce.settings, "calendar_caution_enabled", False), \
             patch.object(ce.settings, "crypto_htf_filter_enabled", False), \
             patch.object(ce.settings, "event_precedent_enabled", False):
            fired = engine.process(self._state())
        self.assertEqual(fired[0].confidence, 0.75)
        self.assertFalse(fired[0].precedent_extended_hold)

    def test_extended_hold_flag_requires_the_hold_setting_and_direction_agreement(self):
        from unittest.mock import patch

        import analysis.crypto_engine as ce

        set_active_briefs({"k": {"window_start": datetime(2025, 1, 1),
                                 "window_end": datetime(2027, 1, 1),
                                 "event_name": "FOMC rate decision", "level": 3,
                                 "sample_size": 3, "confidence_real": "high",
                                 "direction_bias": "bearish"}})
        # Signal is LONG, brief's real lean is bearish -> no extra hold even
        # though the brief is otherwise perfectly usable.
        engine, sig = self._engine_with_one_forced_signal(confidence=0.75, direction="long")
        with patch.object(ce.settings, "orderflow_enabled", False), \
             patch.object(ce.settings, "calendar_caution_enabled", False), \
             patch.object(ce.settings, "crypto_htf_filter_enabled", False), \
             patch.object(ce.settings, "event_precedent_enabled", True), \
             patch.object(ce.settings, "event_precedent_min_sample_size", 2), \
             patch.object(ce.settings, "event_precedent_extended_hold_enabled", True):
            fired = engine.process(self._state())
        self.assertFalse(fired[0].precedent_extended_hold)

        set_active_briefs({"k": {"window_start": datetime(2025, 1, 1),
                                 "window_end": datetime(2027, 1, 1),
                                 "event_name": "FOMC rate decision", "level": 3,
                                 "sample_size": 3, "confidence_real": "high",
                                 "direction_bias": "bullish"}})
        engine, sig = self._engine_with_one_forced_signal(confidence=0.75, direction="long")
        with patch.object(ce.settings, "orderflow_enabled", False), \
             patch.object(ce.settings, "calendar_caution_enabled", False), \
             patch.object(ce.settings, "crypto_htf_filter_enabled", False), \
             patch.object(ce.settings, "event_precedent_enabled", True), \
             patch.object(ce.settings, "event_precedent_min_sample_size", 2), \
             patch.object(ce.settings, "event_precedent_extended_hold_enabled", True):
            fired = engine.process(self._state())
        self.assertTrue(fired[0].precedent_extended_hold)

    def test_no_active_brief_leaves_the_signal_untouched(self):
        from unittest.mock import patch

        import analysis.crypto_engine as ce

        engine, sig = self._engine_with_one_forced_signal(confidence=0.75)
        with patch.object(ce.settings, "orderflow_enabled", False), \
             patch.object(ce.settings, "calendar_caution_enabled", False), \
             patch.object(ce.settings, "crypto_htf_filter_enabled", False), \
             patch.object(ce.settings, "event_precedent_enabled", True):
            fired = engine.process(self._state())   # empty cache, must not raise
        self.assertEqual(fired[0].confidence, 0.75)


class TestPaperCycleExtendedHold(unittest.TestCase):
    """analysis.paper_cycle.open_from_signal's extended_hold_minutes only
    ever raises the hold-time ceiling — never touches the stop or sizing
    the rest of the function already decided."""

    def _cfg(self, max_hold=240):
        from analysis.paper_trading import CycleConfig
        return CycleConfig(max_hold_minutes=max_hold)

    def _sig(self, timeframe="4h"):
        from analysis.crypto_signal import CryptoSignal
        return CryptoSignal(
            symbol="btcusdt", signal_type="confluence", direction="long",
            trigger_description="t", confidence=0.8, current_price=100.0,
            target_price=105.0, stop_loss=98.0, edge_pct=1.0, stake_pct=0.01,
            timeframe=timeframe, sentiment_score=0.0, indicators_summary="",
            timestamp=datetime(2026, 1, 1))

    def test_no_override_behaves_exactly_as_before(self):
        from analysis.paper_cycle import _hold_minutes
        cfg = self._cfg(max_hold=240)
        # 4h timeframe -> 4*90=360 minutes wanted, capped at the 240 ceiling.
        self.assertEqual(_hold_minutes(self._sig("4h"), cfg), 240)

    def test_an_override_raises_the_ceiling_a_normal_signal_would_hit(self):
        from analysis.paper_cycle import _hold_minutes
        cfg = self._cfg(max_hold=240)
        self.assertEqual(_hold_minutes(self._sig("4h"), cfg, ceiling_minutes=4320), 360)

    def test_the_override_is_still_a_ceiling_not_a_floor(self):
        """A short-timeframe signal wanting less than even the extended
        ceiling is unaffected by it."""
        from analysis.paper_cycle import _hold_minutes
        cfg = self._cfg(max_hold=240)
        got = _hold_minutes(self._sig("5m"), cfg, ceiling_minutes=4320)
        self.assertEqual(got, 15.0)   # 5m*1.5=7.5, floored to the 15-minute minimum


class TestPipelineRow(unittest.TestCase):
    """scheduler.settings_page._event_precedent_row: the /api/pipeline
    surface the Settings page's "Why no trades?" panel reads, mirroring
    _calendar_caution_row right beside it."""

    def tearDown(self):
        set_active_briefs({})

    def test_no_active_brief_is_none(self):
        from scheduler.settings_page import _event_precedent_row
        self.assertIsNone(_event_precedent_row(datetime(2026, 10, 1)))

    def test_an_unusable_brief_is_hidden_from_the_page_too(self):
        from scheduler.settings_page import _event_precedent_row
        set_active_briefs({"k": {"window_start": datetime(2025, 1, 1),
                                 "window_end": datetime(2027, 1, 1),
                                 "event_name": "x", "level": 3, "sample_size": 1,
                                 "confidence_real": "high", "direction_bias": "mixed",
                                 "summary": ""}})
        from unittest.mock import patch

        from config.settings import settings
        with patch.object(settings, "event_precedent_min_sample_size", 2):
            self.assertIsNone(_event_precedent_row(datetime(2026, 1, 1)))

    def test_a_usable_brief_surfaces_its_penalty_and_bias(self):
        from scheduler.settings_page import _event_precedent_row
        set_active_briefs({"k": {"window_start": datetime(2025, 1, 1),
                                 "window_end": datetime(2027, 1, 1),
                                 "event_name": "FOMC rate decision", "level": 4,
                                 "sample_size": 3, "confidence_real": "medium",
                                 "direction_bias": "bullish", "summary": "s"}})
        from unittest.mock import patch

        from config.settings import settings
        with patch.object(settings, "event_precedent_min_sample_size", 2):
            row = _event_precedent_row(datetime(2026, 1, 1))
        self.assertEqual(row["name"], "FOMC rate decision")
        self.assertEqual(row["penalty"], 0.06)
        self.assertEqual(row["direction_bias"], "bullish")


if __name__ == "__main__":
    unittest.main()
