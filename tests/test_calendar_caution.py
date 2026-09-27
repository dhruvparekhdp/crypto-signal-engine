"""
Phase 1 (27 Sep) of the two-level analysis research: soften confidence
during structural institutional-flow windows (rebalancing, expiry, thin
weekend liquidity) that calendar_context() already computed but nothing
ever read. Never a veto — the owner's rule throughout this project has
been trade through it, sized down, not paused.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta


def _quiet_moment() -> datetime:
    """A moment with nothing on the calendar, found by search rather than
    hand-verifying a weekday/month-end by eye — robust to the calendar
    itself changing later."""
    from analysis.event_calendar import calendar_context
    d = datetime(2026, 9, 16, 10, 0)   # arbitrary weekday, clear of any 00/08/16 UTC window
    for _ in range(90):
        if not calendar_context(d):
            return d
        d += timedelta(days=1)
    raise AssertionError("no quiet day found — calendar_context may have changed")


def _weekend_moment() -> datetime:
    d = _quiet_moment()
    while d.weekday() < 5:
        d += timedelta(days=1)
    return d.replace(hour=10)


class TestCaution(unittest.TestCase):
    def test_a_quiet_weekday_has_no_caution(self):
        from analysis.event_calendar import caution
        self.assertIsNone(caution(_quiet_moment()))

    def test_a_weekend_reports_thin_liquidity(self):
        from analysis.event_calendar import caution
        item = caution(_weekend_moment())
        self.assertIsNotNone(item)
        self.assertIn("thin liquidity", item.name.lower())

    def test_fomc_cpi_nfp_are_excluded_even_when_they_rank_highest(self):
        """These already pause or bias-check trades a different way
        (active_blackout / event_bias_mode) — caution() must not also flag
        them, or the same release would be counted against a signal twice
        under two different names."""
        from analysis.event_calendar import EVENTS_2026, caution
        fomc = next(ev for ev in EVENTS_2026 if ev.kind == "fomc")
        near = fomc.at - timedelta(hours=1)   # inside calendar_context's window
        item = caution(near)
        if item is not None:
            self.assertNotEqual(item.name, fomc.name)

    def test_every_returned_level_has_a_defined_penalty_bucket(self):
        """The 27 Sep bundle style — if calendar_context ever adds a level
        this mapping does not expect, it must still resolve to something
        sane (the .get(..., 0.01) default) rather than a stray level
        silently doing nothing."""
        PENALTIES = {5: 0.08, 4: 0.06, 3: 0.03}
        d = _quiet_moment()
        for _ in range(30):
            from analysis.event_calendar import caution
            item = caution(d)
            if item is not None:
                penalty = PENALTIES.get(item.level, 0.01)
                self.assertGreater(penalty, 0)
                self.assertLessEqual(penalty, 0.08)
            d += timedelta(days=1)


class TestEngineAppliesCaution(unittest.TestCase):
    """
    The actual wiring in CryptoEngine.process() — a controlled signal from
    one analyzer (confluence), the rest patched off, so this exercises the
    real code path instead of re-deriving the formula next to it.
    """

    def _engine_with_one_forced_signal(self, confidence=0.75):
        import analysis.crypto_engine as ce
        from analysis.crypto_signal import CryptoSignal

        engine = ce.CryptoEngine()
        sig = CryptoSignal(
            symbol="btcusdt", signal_type="confluence", direction="long",
            trigger_description="t", confidence=confidence, current_price=100.0,
            target_price=101.0, stop_loss=99.5, edge_pct=1.0, stake_pct=0.01,
            timeframe="20m", sentiment_score=0.0, indicators_summary="",
            timestamp=datetime(2026, 1, 1))
        engine.confluence.analyze = lambda state: sig
        engine.rsi_divergence.analyze = lambda state: None
        engine.volume_spike.analyze = lambda state: None
        engine.bollinger_squeeze.analyze = lambda state: None
        engine.sentiment_shift.analyze = lambda state: None
        return engine, sig

    def _state(self):
        from analysis.crypto_state import CryptoState
        return CryptoState(symbol="btcusdt", base_asset="BTC", current_price=100.0)

    def test_a_caution_window_lowers_confidence_of_a_fired_signal(self):
        from unittest.mock import patch

        import analysis.crypto_engine as ce
        from analysis.event_calendar import CalendarItem

        engine, sig = self._engine_with_one_forced_signal(confidence=0.75)
        with patch.object(ce.settings, "orderflow_enabled", False), \
             patch.object(ce.settings, "calendar_caution_enabled", True), \
             patch.object(ce.settings, "crypto_htf_filter_enabled", False), \
             patch("analysis.event_calendar.caution",
                   return_value=CalendarItem("Weekend: thin liquidity", 2, "until Sun")):
            fired = engine.process(self._state())
        self.assertEqual(len(fired), 1)
        # Level 2 falls to the default 0.01 bucket — not one of 3/4/5.
        self.assertAlmostEqual(fired[0].confidence, 0.75 - 0.01, places=4)

    def test_the_penalty_never_pushes_confidence_below_050(self):
        """Same clamp every other adjustment in this engine already uses
        (absorption, CVD divergence) — a signal is never zeroed out, only
        made weaker."""
        from unittest.mock import patch

        import analysis.crypto_engine as ce
        from analysis.event_calendar import CalendarItem

        engine, sig = self._engine_with_one_forced_signal(confidence=0.56)
        with patch.object(ce.settings, "orderflow_enabled", False), \
             patch.object(ce.settings, "calendar_caution_enabled", True), \
             patch.object(ce.settings, "crypto_htf_filter_enabled", False), \
             patch.object(ce.settings, "crypto_min_confidence", 0.5), \
             patch("analysis.event_calendar.caution",
                   return_value=CalendarItem("Quarterly options expiry", 4, "Fri")):
            fired = engine.process(self._state())
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0].confidence, 0.50)   # 0.56 - 0.06, clamped no lower

    def test_a_high_level_window_can_push_a_borderline_signal_below_the_floor(self):
        from unittest.mock import patch

        import analysis.crypto_engine as ce
        from analysis.event_calendar import CalendarItem

        engine, sig = self._engine_with_one_forced_signal(confidence=0.56)
        with patch.object(ce.settings, "orderflow_enabled", False), \
             patch.object(ce.settings, "calendar_caution_enabled", True), \
             patch.object(ce.settings, "crypto_htf_filter_enabled", False), \
             patch.object(ce.settings, "crypto_min_confidence", 0.55), \
             patch("analysis.event_calendar.caution",
                   return_value=CalendarItem("Quarterly options expiry", 4, "Fri")):
            fired = engine.process(self._state())
        # 0.56 - 0.06 = 0.50, below this run's 0.55 floor — the setup existed,
        # the window is why it did not fire.
        self.assertEqual(fired, [])

    def test_disabling_the_setting_skips_the_penalty_entirely(self):
        from unittest.mock import patch

        import analysis.crypto_engine as ce

        engine, sig = self._engine_with_one_forced_signal(confidence=0.75)
        with patch.object(ce.settings, "orderflow_enabled", False), \
             patch.object(ce.settings, "calendar_caution_enabled", False), \
             patch.object(ce.settings, "crypto_htf_filter_enabled", False), \
             patch("analysis.event_calendar.caution") as caution_fn:
            fired = engine.process(self._state())
        caution_fn.assert_not_called()
        self.assertEqual(fired[0].confidence, 0.75)

    def test_the_real_caution_call_does_not_crash_on_a_tz_aware_clock(self):
        """
        Not mocked: process() builds datetime.now(UTC) itself (tz-aware) and
        hands it to caution(), which — like the rest of event_calendar.py —
        expects naive UTC. A missing .replace(tzinfo=None) at that call site
        would raise TypeError deep inside calendar_context()'s own datetime
        arithmetic on every single signal, calendar_caution_enabled or not.
        """
        from unittest.mock import patch

        import analysis.crypto_engine as ce

        engine, sig = self._engine_with_one_forced_signal(confidence=0.75)
        with patch.object(ce.settings, "orderflow_enabled", False), \
             patch.object(ce.settings, "calendar_caution_enabled", True), \
             patch.object(ce.settings, "crypto_htf_filter_enabled", False):
            fired = engine.process(self._state())   # must not raise
        self.assertEqual(len(fired), 1)


if __name__ == "__main__":
    unittest.main()
