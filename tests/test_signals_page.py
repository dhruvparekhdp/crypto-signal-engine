"""Command > Signals (owner, 19 Oct 2026: "add filter of won lost on this page and fix styling for veto and viable
chips"). The page said "last 7 days" but the API returned the newest 400 rows - about two days of intraday signals."""
from pathlib import Path
from types import SimpleNamespace

from scheduler.health import _history_limit, _signal_row

PORTAL = Path(__file__).resolve().parents[1] / "scheduler" / "portal"


def _row(**kw):
    base = dict(id=1, symbol="ethusdt", signal_type="confluence", direction="long", confidence=0.61, current_price=100.0,
                target_price=101.0, stop_loss=99.5, edge_pct=0.2, timeframe="5m", outcome="won", pnl_pct=1.0,
                timestamp=None, trade_mode="intraday", veto_reason="", suppressed_by="", skip_reason="shadow_only",
                candidate_role="mirror")
    base.update(kw)
    return SimpleNamespace(**base)


def test_row_says_whether_it_is_the_signal_or_its_mirror():
    assert _signal_row(_row())["role"] == "mirror"
    assert _signal_row(_row(candidate_role="primary"))["role"] == "primary"
    assert _signal_row(_row(candidate_role=None))["role"] == "primary"


def test_history_limit_is_settable_and_capped():
    assert _history_limit({}) == 400
    assert _history_limit({"limit": "2500"}) == 2500
    assert _history_limit({"limit": "999999"}) == 3000
    assert _history_limit({"limit": "x"}) == 400


def test_the_seven_day_table_asks_for_seven_days_of_rows():
    src = (PORTAL / "core.js").read_text()
    line = next(ln for ln in src.splitlines() if 'Store.def("history7"' in ln)
    assert "limit=" in line


def test_signals_table_filters_on_outcome():
    src = (PORTAL / "hubs" / "command.js").read_text()
    assert '["won", "Won"]' in src and '["lost", "Lost"]' in src
