"""Mirror and the intraday simulator as native pages (2 Nov 2026): the numbers they show come from the API."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from scheduler import health
from scheduler.sim_preview import DEFAULTS, preview


class TestSimPreview(unittest.TestCase):
    def test_matches_the_classic_form_maths(self):
        """Same arithmetic the classic page did in the browser: 25 USDT x 25% margin at 10x, 3R / 1R."""
        p = preview({"tp_r": "3", "sl_r": "1", "cycle_start": "25", "cycle_margin_pct": "25", "cycle_leverage": "10",
                     "years": "0.0082", "step_market_hours": "1"})
        t = p["trade"]
        self.assertEqual((t["margin_usdt"], t["position_usdt"]), (6.25, 62.5))
        self.assertEqual((t["target_price_pct"], t["target_roe_pct"], t["target_cash_usdt"]), (3.6, 36.0, 2.25))
        self.assertEqual((t["stop_price_pct"], t["stop_roe_pct"], t["stop_cash_usdt"]), (1.2, 12.0, 0.75))
        self.assertEqual(t["reward_risk"], 3.0)
        self.assertEqual(p["run"], {"market_hours": 72, "steps": 72})

    def test_defaults_and_options_come_from_the_server(self):
        p = preview({})
        self.assertEqual(p["defaults"], DEFAULTS)
        for k in ("strategies", "periods", "ai_providers"):
            self.assertTrue(all("value" in o and "label" in o for o in p["options"][k]))
        self.assertIn("ATR", p["note"])

    def test_junk_input_cannot_produce_absurd_numbers(self):
        p = preview({"tp_r": "abc", "sl_r": "0", "cycle_leverage": "9999", "step_market_hours": "nan", "years": "-1"})
        self.assertEqual(p["trade"]["reward_risk"], 30.0)          # tp default 3 / sl clamped to 0.1
        self.assertEqual(p["cycle"]["leverage"], 125.0)
        self.assertGreaterEqual(p["run"]["steps"], 1)


class TestSimConfigNeverKeepsKeys(unittest.TestCase):
    """The config routes have no login: a key posted to them must not be stored or echoed."""

    def setUp(self):
        self.old = os.getcwd()
        self.dir = tempfile.mkdtemp()
        os.chdir(self.dir)

    def tearDown(self):
        os.chdir(self.old)

    def _req(self, body):
        req = MagicMock()
        req.can_read_body = True

        async def js():
            return body
        req.json = js
        return req

    def test_save_drops_keys_and_get_never_echoes_them(self):
        async def run():
            r = await health._api_simulator_save_config(None, self._req({"tp_r": 2.5, "gemini_key": "AIza-secret", "hf_tokens": "hf_x"}))
            self.assertNotIn("AIza-secret", r.text)
            saved = json.loads(Path("data/simulator/config.json").read_text())
            self.assertNotIn("gemini_key", saved)
            self.assertEqual(saved["tp_r"], 2.5)
            # a file written before the fix still has a key: GET must blank it
            Path("data/simulator/config.json").write_text(json.dumps({"openrouter_key": "sk-old", "tp_r": 2.5}))
            g = json.loads((await health._api_simulator_get_config(None, MagicMock())).text)
            self.assertEqual(g["openrouter_key"], "")
            self.assertEqual(g["tp_r"], 2.5)
        asyncio.run(run())


class TestMirrorFieldsFromTheServer(unittest.TestCase):
    def test_level_distance(self):
        self.assertEqual(health._level_pct(100, 103), 3.0)
        self.assertEqual(health._level_pct(200, 190), 5.0)
        self.assertIsNone(health._level_pct(100, 0))
        self.assertIsNone(health._level_pct(None, 5))
        self.assertIsNone(health._level_pct("x", 5))


if __name__ == "__main__":
    unittest.main()
