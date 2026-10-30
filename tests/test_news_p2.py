"""P2 events pipeline tests."""
from __future__ import annotations

import unittest
from datetime import datetime

from analysis.news.classifier import classify_rule_based
from analysis.news.dedupe import mark_doc_duplicates
from analysis.news.impulse_link import events_open_at, impulse_row_key
from analysis.news.market_severity import market_severity_from_btc_move
from analysis.news.normalize import document_external_id
from analysis.news.resolver import event_key_for, is_followup


class TestNewsP2(unittest.TestCase):
    def test_classifier_fomc_is_central_bank_severity(self):
        c = classify_rule_based("FOMC rate decision surprises markets")
        self.assertEqual(c["event_type"], "central_bank")
        self.assertGreaterEqual(c["severity"], 3)
        self.assertIn("fed", c["institutions"])

    def test_dedupe_marks_second_headline(self):
        docs = [
            {"external_id": "a", "title": "Bitcoin ETF sees inflows", "published_at": datetime(2026, 1, 1, 12, 0)},
            {"external_id": "b", "title": "Bitcoin ETF sees inflows!", "published_at": datetime(2026, 1, 1, 12, 5)},
        ]
        out = mark_doc_duplicates(docs, threshold=0.99, window_hours=12.0)
        self.assertEqual(out[1]["duplicate_of"], "a")

    def test_resolver_followup(self):
        self.assertTrue(is_followup("FOMC statement: rates held", "FOMC statement"))

    def test_market_severity_btc(self):
        self.assertEqual(market_severity_from_btc_move(2.5), 4)

    def test_impulse_link_open_events(self):
        at = datetime(2026, 10, 30, 12, 0)
        evs = [{"id": 7, "status": "open", "started_at": at.isoformat()}]
        self.assertEqual(events_open_at(evs, at), [7])

    def test_document_external_id_stable(self):
        self.assertEqual(
            document_external_id("rss", "Hello", "http://x"),
            document_external_id("rss", "Hello", "http://x"),
        )

    def test_event_key_stable(self):
        k1 = event_key_for("War outbreak", "geopolitical", datetime(2026, 1, 2))
        k2 = event_key_for("War outbreak", "geopolitical", datetime(2026, 1, 2))
        self.assertEqual(k1, k2)
