"""17 Oct 2026: Binance spot archive files from 2025 carry open_time in microseconds; the lab converts them to ms
instead of refusing the symbol, and still refuses genuinely bad clocks."""
import pandas as pd
import pytest

from analysis.lab import data as D


def _write(tmp_path, rows):
    d = tmp_path / "um" / "klines" / "TESTUSDT" / "4h"
    d.mkdir(parents=True)
    pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "taker_buy_volume"]).to_parquet(d / "x.parquet")


def test_microsecond_rows_are_converted(tmp_path):
    ms, us = 1_733_011_200_000, 1_767_225_600_000 * 1000
    _write(tmp_path, [[ms, 1, 1, 1, 1, 1, 0], [us, 1, 1, 1, 1, 1, 0]])
    df = D._read_dir(tmp_path, "um", "TESTUSDT", "4h")
    assert list(df.open_time) == [ms, us // 1000]


def test_seconds_still_refused(tmp_path):
    _write(tmp_path, [[1_733_011_200, 1, 1, 1, 1, 1, 0]])
    with pytest.raises(D.MissingData):
        D._read_dir(tmp_path, "um", "TESTUSDT", "4h")
