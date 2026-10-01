"""
Unit tests for GlobalAIGovernor and rate-limiting / event logging logic.
"""
import asyncio
import time
import unittest
from analysis.simulator.global_ai_governor import GlobalAIGovernor


class TestGlobalAIGovernor(unittest.IsolatedAsyncioTestCase):
    async def test_governor_rpm_and_spacing(self):
        gov = GlobalAIGovernor(events_log_path="data/simulator/test_ai_events.json")
        gov.quotas["test_prov"] = gov.quotas["groq"].__class__(
            rpm_limit=3,
            rpd_limit=10,
            min_interval_seconds=0.05,
        )

        # First call succeeds
        self.assertTrue(await gov.reserve_call("test_prov"))

        # Immediate second call fails due to min_interval
        self.assertFalse(await gov.can_call_provider("test_prov"))

        # Wait for interval
        await asyncio.sleep(0.06)
        self.assertTrue(await gov.can_call_provider("test_prov"))
        self.assertTrue(await gov.reserve_call("test_prov"))

        # Third call
        await asyncio.sleep(0.06)
        self.assertTrue(await gov.reserve_call("test_prov"))

        # 4th call within same minute exceeds RPM (3)
        await asyncio.sleep(0.06)
        self.assertFalse(await gov.can_call_provider("test_prov"))
        self.assertFalse(await gov.reserve_call("test_prov"))

    def test_governor_event_recording(self):
        gov = GlobalAIGovernor(events_log_path="data/simulator/test_ai_events.json")
        evt = gov.record_event(
            provider="gemini",
            model="gemini-3-flash-preview",
            call_type="dual_post_mortem",
            symbol="BTCUSDT",
            direction="LONG",
            latency_ms=850,
            status="SUCCESS",
            summary="BTC Long TP Hit",
            why_it_worked="Momentum expansion",
            why_it_failed="None",
            key_takeaway="Let runners extend",
            thinking_trace="<think>Macro Bullish</think>",
        )

        self.assertTrue(evt.event_id.startswith("ai-evt-"))
        self.assertEqual(evt.provider, "gemini")
        self.assertEqual(evt.latency_ms, 850)
        self.assertGreaterEqual(len(gov.events), 1)

        telemetry = gov.get_telemetry()
        self.assertIn("gemini", telemetry["providers"])
        self.assertGreaterEqual(telemetry["total_events_logged"], 1)


if __name__ == "__main__":
    unittest.main()
