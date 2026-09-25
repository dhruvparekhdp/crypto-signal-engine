"""The server's own v2 backtest: download (mocked), test, save, serve."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd
from aiohttp.test_utils import make_mocked_request

from collectors.binance_lake import Part, write_month
from tests.test_v2 import market


class TestServerBacktest(unittest.TestCase):
    def test_job_writes_a_report_the_page_can_read(self):
        with tempfile.TemporaryDirectory() as d:
            lake, reports = Path(d, "lake"), Path(d, "reports")
            end = pd.Timestamp.now("UTC").tz_localize(None).normalize()
            frames = market(90, seed=3)
            shift = (end - frames[0].ts.max() - pd.Timedelta(days=1)).floor("D")
            for iv, df in zip(("5m", "15m", "4h", "1d"), frames, strict=True):
                df = df.assign(ts=df.ts + shift)
                for m, g in df.groupby(df.ts.dt.strftime("%Y-%m")):
                    write_month(lake, Part("um", "klines", "BTCUSDT", iv, m), [g])

            from scheduler.runner import AppRunner
            runner = SimpleNamespace(
                _v2_bt_state={},
                crypto_store=SimpleNamespace(get_symbols=AsyncMock(return_value=["btcusdt"])))
            job = AppRunner._v2_backtest_job.__get__(runner)

            async def run():
                from config.settings import settings
                with patch.object(settings, "v2_lake_dir", str(lake)), \
                     patch.object(settings, "v2_reports_dir", str(reports)), \
                     patch.object(settings, "v2_backtest_years", 0.2), \
                     patch("scripts.load_binance_lake.load", AsyncMock(return_value=0)):
                    await job()
                    self.assertFalse(runner._v2_bt_state.get("running"))
                    self.assertNotIn("error", runner._v2_bt_state)
                    from scheduler.v2_pages import backtest_api
                    get, _ = backtest_api(runner)
                    resp = await get(make_mocked_request("GET", "/api/v2/backtest"))
                    return json.loads(resp.text)

            body = asyncio.run(run())
            r = body["report"]
            self.assertEqual(r["symbols"], ["BTCUSDT"])
            self.assertIn("A", r["setups"])
            self.assertIn("long", r["by_side"])
            self.assertIn("BTCUSDT", r["by_symbol"])
            self.assertNotIn("trades", r)
            self.assertTrue((reports / "backtest_v2_latest.json").exists())


if __name__ == "__main__":
    unittest.main()
