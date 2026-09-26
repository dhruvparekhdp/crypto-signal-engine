"""The Parquet lake of Binance's public archive: URLs, parsing, storage."""

import hashlib
import io
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

from collectors.binance_lake import (
    Part,
    parse_csv,
    plan,
    read,
    unzip_verified,
    write_month,
)


class TestPlan(unittest.TestCase):
    def test_urls_match_the_archive_layout(self):
        p = Part("um", "klines", "BTCUSDT", "1m", "2024-01")
        self.assertEqual(p.url, "https://data.binance.vision/data/futures/um/monthly/klines/"
                                "BTCUSDT/1m/BTCUSDT-1m-2024-01.zip")
        p = Part("um", "metrics", "BTCUSDT", "", "2024-01-05")
        self.assertEqual(p.url, "https://data.binance.vision/data/futures/um/daily/metrics/"
                                "BTCUSDT/BTCUSDT-metrics-2024-01-05.zip")
        p = Part("spot", "klines", "ETHUSDT", "1s", "2025-02")
        self.assertIn("/data/spot/monthly/klines/ETHUSDT/1s/ETHUSDT-1s-2025-02.zip", p.url)

    def test_whole_months_monthly_current_month_daily(self):
        end = date.today() - timedelta(days=1)
        start = date(end.year - 1, end.month, 1)
        parts = plan("um", "klines", "btcusdt", "1h", start, end)
        monthly = [p for p in parts if not p.daily]
        daily = [p for p in parts if p.daily]
        self.assertEqual(len(monthly), 12)
        self.assertEqual(len(daily), end.day if end.month == date.today().month else 0)
        self.assertTrue(all(p.symbol == "BTCUSDT" for p in parts))

    def test_metrics_are_daily_funding_monthly(self):
        parts = plan("um", "metrics", "BTCUSDT", "", date(2024, 1, 1), date(2024, 1, 31))
        self.assertEqual(len(parts), 31)
        self.assertEqual(plan("um", "fundingRate", "BTCUSDT", "5m",
                              date(2024, 1, 1), date(2024, 3, 31))[0].interval, "")

    def test_impossible_combinations_are_refused(self):
        with self.assertRaises(ValueError):
            plan("um", "klines", "BTCUSDT", "1s", date(2024, 1, 1), date(2024, 2, 1))
        with self.assertRaises(ValueError):
            plan("spot", "metrics", "BTCUSDT", "", date(2024, 1, 1), date(2024, 2, 1))


class TestParse(unittest.TestCase):
    def test_futures_klines_with_header(self):
        raw = (b"open_time,open,high,low,close,volume,close_time,quote_volume,count,"
               b"taker_buy_volume,taker_buy_quote_volume,ignore\n"
               b"1704067200000,42000.1,42100,41950,42050,12.5,1704067259999,525000,300,"
               b"7.1,298000,0\n")
        df = parse_csv("klines", raw)
        self.assertEqual(len(df), 1)
        self.assertEqual(str(df.ts[0]), "2024-01-01 00:00:00")
        self.assertAlmostEqual(df.close[0], 42050.0)
        self.assertAlmostEqual(df.taker_buy_volume[0], 7.1)
        self.assertNotIn("ignore", df.columns)

    def test_spot_microseconds_without_header(self):
        raw = b"1735689600000000,1,2,0.5,1.5,10,1735689600999999,15,3,4,6,0\n"
        df = parse_csv("klines", raw)
        self.assertEqual(str(df.ts[0]), "2025-01-01 00:00:00")

    def test_metrics_and_depth_use_date_strings(self):
        raw = (b"create_time,symbol,sum_open_interest,sum_open_interest_value,"
               b"count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
               b"count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
               b"2024-01-01 00:05:00,BTCUSDT,80000.5,3.4e9,1.2,1.1,1.3,0.95\n")
        df = parse_csv("metrics", raw)
        self.assertEqual(str(df.ts[0]), "2024-01-01 00:05:00")
        self.assertAlmostEqual(df.sum_open_interest[0], 80000.5)
        raw = b"timestamp,percentage,depth,notional\n2024-01-01 00:00:08,-1,1500.2,6.3e7\n"
        df = parse_csv("bookDepth", raw)
        self.assertEqual(df.percentage[0], -1)

    def test_funding(self):
        raw = b"calc_time,funding_interval_hours,last_funding_rate\n1704067200000,8,0.0001\n"
        df = parse_csv("fundingRate", raw)
        self.assertAlmostEqual(df.last_funding_rate[0], 0.0001)

    def test_checksum_is_enforced(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a.csv", "x")
        blob = buf.getvalue()
        good = hashlib.sha256(blob).hexdigest() + "  a.zip"
        self.assertEqual(unzip_verified(blob, good), b"x")
        with self.assertRaises(ValueError):
            unzip_verified(blob, "0" * 64 + "  a.zip")


class TestStore(unittest.TestCase):
    def test_write_is_idempotent_and_read_slices(self):
        raw = b"".join(
            f"{1704067200000 + i * 60000},1,2,0.5,1.5,10,0,15,3,4,6,0\n".encode()
            for i in range(10))
        df = parse_csv("klines", raw)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            part = Part("um", "klines", "BTCUSDT", "1m", "2024-01")
            write_month(root, part, [df])
            write_month(root, part, [df])          # a re-run adds nothing
            got = read("klines", "BTCUSDT", "2024-01-01 00:02", "2024-01-01 00:05",
                       interval="1m", root=root)
            self.assertEqual(len(got), 3)
            full = read("klines", "BTCUSDT", "2024-01-01", "2024-02-01", interval="1m",
                        root=root)
            self.assertEqual(len(full), 10)


if __name__ == "__main__":
    unittest.main()


class TestRobustness(unittest.TestCase):
    def test_a_corrupt_month_is_set_aside_not_fatal(self):
        raw = b"1704067200000,1,2,0.5,1.5,10,0,15,3,4,6,0\n"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            part = Part("um", "klines", "BTCUSDT", "1m", "2024-01")
            path = write_month(root, part, [parse_csv("klines", raw)])
            path.write_bytes(b"half a parquet")                 # killed mid-write
            self.assertTrue(read("klines", "BTCUSDT", "2024-01-01", "2024-02-01",
                                 interval="1m", root=root).empty)
            self.assertTrue(path.with_name(path.name + ".corrupt").exists())
            write_month(root, part, [parse_csv("klines", raw)])  # recovers
            self.assertEqual(len(read("klines", "BTCUSDT", "2024-01-01", "2024-02-01",
                                      interval="1m", root=root)), 1)
            self.assertFalse(any(p.suffix == ".tmp" for p in path.parent.iterdir()))

    def test_a_just_finished_month_comes_as_daily_files(self):
        from unittest.mock import patch

        import collectors.binance_lake as lake
        class Day(date):
            @classmethod
            def today(cls):
                return date(2026, 10, 3)
        with patch.object(lake, "date", Day):
            parts = plan("um", "klines", "BTCUSDT", "1h", date(2026, 9, 1), date(2026, 10, 2))
            self.assertTrue(all(p.daily for p in parts))          # Sept monthly not out yet
            self.assertEqual(plan("um", "fundingRate", "BTCUSDT", "", date(2026, 9, 1),
                                  date(2026, 10, 2)), [])         # REST top-up covers it
            parts = plan("um", "klines", "BTCUSDT", "1h", date(2026, 7, 1), date(2026, 10, 2))
            self.assertIn("2026-07", [p.period for p in parts])
            self.assertIn("2026-08", [p.period for p in parts])
