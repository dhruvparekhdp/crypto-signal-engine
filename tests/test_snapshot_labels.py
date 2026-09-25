"""
The labels the training table never had.

`crypto_snapshots` has carried price_30m_later, price_1h_later,
price_4h_later and price_1d_later since the schema was written, and nothing
has ever written to any of them — every row in a 35,637-row table holds the
0.0 column default. The one table built to be a training set has features and
no labels.

No new collection fixes that. The price half an hour after the 10:00 snapshot
is sitting in the 10:30 snapshot; the label is a self-join nobody did. These
tests pin the join, and in particular the three ways it could quietly produce
a wrong label instead of no label — crossing symbols, accepting a far-away
row, and labelling a row whose future has not happened yet.
"""
import unittest
from datetime import UTC, datetime, timedelta

from analysis.snapshot_labeler import HORIZONS, TOLERANCE, label_rows


class _Snap:
    """Stands in for CryptoSnapshot; label_rows only touches these fields."""

    def __init__(self, symbol, price, timestamp):
        self.symbol, self.price, self.timestamp = symbol, price, timestamp
        for column in HORIZONS:
            setattr(self, column, 0.0)


T0 = datetime(2026, 9, 20, 10, 0)
NOW = T0 + timedelta(days=3)


def _series(symbol="btcusdt", minutes=300, step=2, price=lambda i: 80000.0 + i):
    """A snapshot every `step` minutes, the way the collector writes them."""
    return [_Snap(symbol, price(i), T0 + timedelta(minutes=i * step))
            for i in range(minutes // step)]


class TestTheJoin(unittest.TestCase):
    def test_a_snapshot_learns_what_the_price_became(self):
        rows = _series()
        label_rows(rows, NOW)
        first = rows[0]
        # 30 minutes on from index 0, at a 2-minute step, is index 15.
        self.assertEqual(first.price_30m_later, rows[15].price)
        self.assertEqual(first.price_1h_later, rows[30].price)

    def test_the_label_is_a_price_not_a_return(self):
        """The column says `price_..._later`. Returns are the reader's choice."""
        rows = _series(price=lambda i: 100.0 + i)
        label_rows(rows, NOW)
        self.assertEqual(rows[0].price_30m_later, 115.0)

    def test_rows_already_labelled_are_left_alone(self):
        rows = _series()
        rows[0].price_30m_later = 12345.0
        label_rows(rows, NOW)
        self.assertEqual(rows[0].price_30m_later, 12345.0)

    def test_running_twice_changes_nothing_the_second_time(self):
        rows = _series()
        first = label_rows(rows, NOW)
        second = label_rows(rows, NOW)
        self.assertGreater(first.labelled, 0)
        self.assertEqual(second.labelled, 0)


class TestTheWaysItCouldBeWrong(unittest.TestCase):
    """Each of these would produce a plausible-looking wrong number."""

    def test_one_symbol_never_borrows_another_symbol_price(self):
        """
        BTC's price half an hour after an ETH snapshot is not a label. Both
        series are interleaved in one query result, so the separation has to
        happen here or the table fills with cross-symbol nonsense.
        """
        rows = _series("btcusdt", price=lambda i: 80000.0 + i) \
             + _series("ethusdt", price=lambda i: 2500.0 + i)
        label_rows(rows, NOW)
        for row in rows:
            if row.price_30m_later:
                near = 80000 if row.symbol == "btcusdt" else 2500
                self.assertLess(abs(row.price_30m_later - near), 1000,
                                f"{row.symbol} got a label from the other series")

    def test_a_gap_leaves_the_label_empty_rather_than_reaching_for_it(self):
        """
        A restart leaves a hole. Stitching the label from whatever row is
        nearest — an hour off the horizon — is worse than no label, because
        nothing downstream can tell it is wrong.
        """
        rows = _series(minutes=600)
        kept = [r for r in rows
                if not (timedelta(minutes=25) <= r.timestamp - T0 <= timedelta(minutes=95))]
        run = label_rows(kept, NOW)
        self.assertEqual(kept[0].price_30m_later, 0.0)
        self.assertGreater(run.unresolved, 0)

    def test_a_row_whose_future_has_not_happened_is_not_labelled(self):
        """
        Looking up the nearest row to a target in the future finds the last
        row in the series, which is not the price at that horizon — it is the
        price now. That would be lookahead pointing the wrong way.
        """
        rows = _series(minutes=300)
        just_after = rows[-1].timestamp + timedelta(minutes=1)
        label_rows(rows, just_after)
        self.assertEqual(rows[-1].price_30m_later, 0.0)
        self.assertEqual(rows[-1].price_1d_later, 0.0)

    def test_a_zero_price_is_not_a_label(self):
        """
        Gold wrote 0.0 for 55 of its 5,091 rows when TwelveData went quiet. A
        0.0 label makes the return -100%, which is how a dead feed turns into
        a trading signal.
        """
        rows = _series()
        rows[15].price = 0.0
        run = label_rows(rows, NOW)
        self.assertEqual(rows[0].price_30m_later, 0.0)
        self.assertGreater(run.unresolved, 0)

    def test_tolerance_is_tighter_than_the_gap_between_horizons(self):
        """Otherwise the 30m and 1h labels could resolve to the same row."""
        self.assertLess(TOLERANCE * 2, HORIZONS["price_1h_later"] - HORIZONS["price_30m_later"])


class TestReporting(unittest.TestCase):
    def test_coverage_says_how_much_of_the_due_work_landed(self):
        rows = _series(minutes=600)
        kept = [r for r in rows
                if not (timedelta(minutes=25) <= r.timestamp - T0 <= timedelta(minutes=95))]
        run = label_rows(kept, NOW)
        self.assertGreater(run.coverage, 0.0)
        self.assertLess(run.coverage, 1.0)

    def test_coverage_is_not_a_division_by_zero_when_nothing_was_due(self):
        rows = _series(minutes=10)
        self.assertEqual(label_rows(rows, T0).coverage, 0.0)


class TestTimezones(unittest.TestCase):
    def test_aware_and_naive_timestamps_both_work(self):
        """SQLite returns naive datetimes and Postgres aware ones."""
        rows = [_Snap("btcusdt", 80000.0 + i,
                      (T0 + timedelta(minutes=i * 2)).replace(tzinfo=UTC))
                for i in range(150)]
        run = label_rows(rows, NOW.replace(tzinfo=UTC))
        self.assertGreater(run.labelled, 0)


class TestRetention(unittest.TestCase):
    """
    The cleanup job deleted snapshots after three days, on the same setting
    as the signal log. Three days is right for a log you read when something
    breaks; for the only table carrying features and labels together it meant
    no model could ever be fitted on more than three days, however long the
    engine ran.
    """

    def test_the_signal_log_outlives_every_window_that_reads_it(self):
        """
        The null test reads 120 days of signals and the accuracy page 365. A
        shorter retention silently shrinks both, and deletes a restore of
        older signals at the next cleanup.
        """
        from config.settings import settings

        self.assertGreaterEqual(settings.signal_log_retention_days, 365)

    def test_retention_covers_the_longest_label_horizon_many_times_over(self):
        """A window barely longer than the horizon yields almost no usable rows."""
        from config.settings import settings

        longest_days = max(HORIZONS.values()).total_seconds() / 86400
        self.assertGreater(settings.snapshot_retention_days, longest_days * 30)

    def test_the_two_retentions_are_applied_separately(self):
        """One shared cutoff is how the training set got deleted with the logs."""
        import inspect

        from storage.repository import Repository

        source = inspect.getsource(Repository.delete_old_crypto_data)
        self.assertIn("snap_cutoff", source)
        self.assertIn("log_cutoff", source)


class TestTheRoutinePassIsBounded(unittest.TestCase):
    """
    The job runs every fifteen minutes. At 120 days of retention the table
    holds around 600,000 rows, and loading every one of them four times an
    hour to write a few hundred labels is most of a small database's day
    spent on nothing.
    """

    def test_the_window_covers_the_longest_horizon_with_slack(self):
        from analysis.snapshot_labeler import ROUTINE_WINDOW

        self.assertGreater(ROUTINE_WINDOW, max(HORIZONS.values()))

    def test_the_window_is_days_not_months(self):
        """A row older than a day plus slack has already had its answer decided."""
        from analysis.snapshot_labeler import ROUTINE_WINDOW

        self.assertLessEqual(ROUTINE_WINDOW, timedelta(days=7))

    def test_the_scheduled_job_does_not_ask_for_the_retention_window(self):
        """
        It did, briefly, which would have loaded 120 days of rows every
        fifteen minutes the moment retention was raised.
        """
        from pathlib import Path

        runner = (Path(__file__).resolve().parent.parent / "scheduler/runner.py").read_text()
        self.assertIn("await backfill_labels(session)", runner)
        self.assertNotIn("backfill_labels(session, lookback_days", runner)


class TestTheFullPassAfterARestore(unittest.TestCase):
    """
    Rows restored from a backup are historical by definition — outside the
    routine window the moment they land — so the scheduled job would never
    touch them, however long the engine ran.
    """

    def _history(self, days=20, step_minutes=2):
        start = T0 - timedelta(days=days)
        count = int(days * 24 * 60 / step_minutes)
        return [_Snap("btcusdt", 80000.0 + i,
                      start + timedelta(minutes=i * step_minutes))
                for i in range(count)]

    def test_chunks_overlap_by_at_least_the_longest_horizon(self):
        """
        Without the overlap every chunk edge produces a day of spuriously
        unresolved labels, because the row that would label them sits in the
        next chunk.
        """
        import inspect

        from analysis.snapshot_labeler import backfill_all

        source = inspect.getsource(backfill_all)
        self.assertIn("overlap = max(HORIZONS.values())", source)
        self.assertIn("end + overlap", source)

    def test_labelling_a_long_history_in_chunks_matches_one_pass(self):
        """The chunking is an implementation detail; the labels must not be."""
        rows = self._history()
        one_pass = label_rows(rows, T0 + timedelta(days=2))

        fresh = self._history()
        by_time = sorted(fresh, key=lambda r: r.timestamp)
        chunked = 0
        overlap = max(HORIZONS.values())
        start = by_time[0].timestamp
        while start <= by_time[-1].timestamp:
            end = start + timedelta(days=7)
            window = [r for r in by_time if start <= r.timestamp < end + overlap]
            chunked += label_rows(window, T0 + timedelta(days=2)).labelled
            start = end

        self.assertEqual(chunked, one_pass.labelled)

    def test_it_commits_per_chunk_rather_than_once_at_the_end(self):
        """
        One transaction holding 600,000 dirty ORM objects is a memory problem
        on the box and a lock problem on the database.
        """
        import inspect

        from analysis.snapshot_labeler import backfill_all

        source = inspect.getsource(backfill_all)
        self.assertIn("while start < now", source)
        self.assertIn("await session.commit()", source)

    def test_a_script_exists_for_running_it(self):
        from pathlib import Path

        script = Path(__file__).resolve().parent.parent / "scripts/backfill_labels.py"
        self.assertTrue(script.exists())
        self.assertIn("backfill_all", script.read_text())
