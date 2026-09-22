"""
Filling years of history, and knowing whether it worked.

Two things make this worth testing rather than trusting. The first is that a
backfill is re-run constantly — interrupted, resumed, overlapped with a
different date range — so "insert only what is new" is the whole contract.
The second is that an empty result and an unreachable exchange look identical
to a caller that only sees a list, and they mean opposite things.
"""
import unittest
from datetime import UTC, datetime, timedelta

from collectors.binance_history import (
    INTERVAL_SECONDS,
    _months,
    _row_to_candle,
    expected_bars,
    interval_delta,
)

ROW = [1700000000000, "100.0", "110.0", "90.0", "105.0", "12.5",
       1700000059999, "1300.0", "42", "6.0", "600.0", "0"]


class TestParsingABar(unittest.TestCase):
    """One parser for both transports — the CSV and the REST array agree."""

    def test_the_twelve_fields_land_where_they_belong(self):
        c = _row_to_candle(ROW, "btcusdt", "1m", "test")
        self.assertEqual((c["open"], c["high"], c["low"], c["close"]),
                         (100.0, 110.0, 90.0, 105.0))
        self.assertEqual(c["volume"], 12.5)
        self.assertEqual(c["trades"], 42)
        self.assertEqual(c["symbol"], "btcusdt")

    def test_the_timestamp_is_naive_utc(self):
        """The whole schema is TIMESTAMP WITHOUT TIME ZONE; a tz-aware value
        raises on comparison against everything else in the table."""
        self.assertIsNone(_row_to_candle(ROW, "btcusdt", "1m", "t")["open_time"].tzinfo)

    def test_microsecond_stamps_are_recognised(self):
        """Binance's archives switched units partway through 2025."""
        micro = [1700000000000000] + ROW[1:]
        self.assertEqual(_row_to_candle(micro, "b", "1m", "t")["open_time"],
                         _row_to_candle(ROW, "b", "1m", "t")["open_time"])

    def test_a_zero_price_is_rejected_not_stored(self):
        """
        Gold arrived priced at 4.3e-05 for 55 snapshots once. A row like that
        in a denominator turns a 0.04% move into 15 million percent.
        """
        self.assertIsNone(_row_to_candle([ROW[0], "0", "110", "90", "105", "1"],
                                         "b", "1m", "t"))

    def test_a_high_below_its_low_is_rejected(self):
        self.assertIsNone(_row_to_candle([ROW[0], "100", "90", "110", "105", "1"],
                                         "b", "1m", "t"))

    def test_junk_is_skipped_rather_than_raised(self):
        """One bad line in a month of CSV must not lose the month."""
        for junk in ([], ["header", "row"], None, [ROW[0]]):
            with self.subTest(junk=junk):
                self.assertIsNone(_row_to_candle(junk, "b", "1m", "t"))


class TestRangeArithmetic(unittest.TestCase):
    def test_months_are_inclusive_at_both_ends(self):
        self.assertEqual(_months(datetime(2025, 11, 5).date(), datetime(2026, 2, 3).date()),
                         [(2025, 11), (2025, 12), (2026, 1), (2026, 2)])

    def test_a_single_month_is_one_download(self):
        self.assertEqual(_months(datetime(2026, 3, 2).date(), datetime(2026, 3, 28).date()),
                         [(2026, 3)])

    def test_bars_in_a_day(self):
        day = (datetime(2026, 1, 1), datetime(2026, 1, 2))
        self.assertEqual(expected_bars(*day, "1m"), 1440)
        self.assertEqual(expected_bars(*day, "5m"), 288)
        self.assertEqual(expected_bars(*day, "1h"), 24)

    def test_an_unknown_interval_is_refused_loudly(self):
        """Silently treating '1min' as something would store a mislabelled series."""
        with self.assertRaises(ValueError):
            interval_delta("1min")

    def test_every_interval_has_a_delta(self):
        for interval in INTERVAL_SECONDS:
            with self.subTest(interval=interval):
                self.assertEqual(interval_delta(interval),
                                 timedelta(seconds=INTERVAL_SECONDS[interval]))


class TestUnreachableIsNotEmpty(unittest.TestCase):
    """
    The distinction this module exists to preserve. "Binance has no bars for
    2019 because the pair was not listed" is a finished answer; "every host
    refused the connection" is a run that did no work. A caller seeing an
    empty list for both discovers a week later that the table is empty.
    """

    def test_the_exception_type_exists_and_is_catchable(self):
        from collectors.binance_history import BinanceUnreachable

        self.assertTrue(issubclass(BinanceUnreachable, Exception))

    def test_a_404_is_the_archive_answering_not_a_failure(self):
        """
        Every backfill hits 404s at both ends of its range — before the pair
        listed, and for the current month. Counting those as outages would
        make every successful run look broken.
        """
        import inspect

        from collectors.binance_history import BinanceHistory

        source = inspect.getsource(BinanceHistory._archive_month)
        after_404 = source[source.index("status_code == 404"):
                           source.index("status_code != 200")]
        self.assertNotIn("transport_failures", after_404)

    def test_a_4xx_from_the_rest_endpoint_also_counts_as_answered(self):
        """A bad symbol is Binance replying, not Binance being down."""
        import inspect

        from collectors.binance_history import BinanceHistory

        self.assertIn("400 <= resp.status_code < 500",
                      inspect.getsource(BinanceHistory._rest_range))

    def test_it_only_raises_when_nothing_at_all_answered(self):
        """
        One flaky month inside an otherwise working run is not an outage —
        raising there would throw away the bars that did arrive.
        """
        import inspect

        from collectors.binance_history import BinanceHistory

        self.assertIn("not yielded_any and self.transport_failures and not self.responses_seen",
                      inspect.getsource(BinanceHistory.fetch_range))


class TestNoKeyIsNeeded(unittest.TestCase):
    def test_nothing_here_reads_an_api_key(self):
        """
        Binance market data is public. A backfill that waits on a key the
        operator has not added yet is a backfill that does not run.
        """
        from pathlib import Path

        source = (Path(__file__).resolve().parent.parent
                  / "collectors/binance_history.py").read_text()
        for marker in ("api_key", "API_KEY", "Authorization", "X-MBX-APIKEY", "signature"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source)


class TestTheScriptIsUsableBeforeItIsRun(unittest.TestCase):
    def setUp(self):
        from pathlib import Path

        self.source = (Path(__file__).resolve().parent.parent
                       / "scripts/backfill_candles.py").read_text()

    def test_it_can_estimate_the_size_without_fetching(self):
        """
        Millions of rows is a decision about the database, and finding out
        afterwards is the wrong time.
        """
        self.assertIn("--dry-run", self.source)
        self.assertIn("MB on disk", self.source)

    def test_it_can_report_what_is_already_stored(self):
        self.assertIn("--status", self.source)

    def test_a_wholly_unreachable_run_exits_non_zero(self):
        """So a cron entry fails visibly instead of logging a cheerful zero."""
        self.assertIn("every one of the", self.source)
        self.assertIn("return 1", self.source)


class TestDuplicatesAreImpossibleRatherThanFiltered(unittest.TestCase):
    """
    A backfill is re-run constantly — interrupted, resumed, overlapped with a
    different date range, run twice by two people. The unique constraint is
    what makes that safe, and it has to be the database's job: filtering in
    application code means every future writer has to remember to, and the
    window between a SELECT and an INSERT is exactly where a retry lands.
    """

    def test_the_constraint_is_on_the_bar_identity(self):
        from storage.models import MarketCandle

        unique = [c for c in MarketCandle.__table__.constraints
                  if c.__class__.__name__ == "UniqueConstraint"]
        self.assertEqual(len(unique), 1)
        self.assertEqual({c.name for c in unique[0].columns},
                         {"symbol", "interval", "open_time"})

    def test_the_writer_leaves_existing_rows_alone(self):
        """
        DO NOTHING, not DO UPDATE. A closed bar is final — Binance does not
        revise them — so an update would rewrite millions of rows to change
        nothing.
        """
        import inspect

        from storage.repository import Repository

        source = inspect.getsource(Repository.save_candles)
        self.assertIn("on_conflict_do_nothing", source)
        self.assertNotIn("on_conflict_do_update", source)

    def test_it_names_columns_rather_than_the_constraint(self):
        """SQLite's dialect accepts only index_elements; the tests run there."""
        import inspect

        from storage.repository import Repository

        self.assertIn("index_elements", inspect.getsource(Repository.save_candles))

    def test_the_key_autoincrements_on_both_backends(self):
        """
        Only INTEGER PRIMARY KEY aliases the rowid on SQLite, so a plain
        BigInteger key is a NOT NULL column with no default and every insert
        fails there while working fine on Postgres.
        """
        from sqlalchemy.dialects import postgresql, sqlite

        from storage.models import MarketCandle

        key = MarketCandle.__table__.c.id
        self.assertTrue(key.autoincrement)
        self.assertEqual(str(key.type.compile(sqlite.dialect())), "INTEGER")
        self.assertEqual(str(key.type.compile(postgresql.dialect())), "BIGINT")
