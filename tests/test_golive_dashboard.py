"""Go-live dashboard (15 Oct 2026): the verdict and gates come from the server's real state, never optimistic."""
from scheduler.golive_api import evaluate, journal_stats

NOW = 1_792_100_000_000
DAY = 86_400_000


def _snap(**kw):
    j = journal_stats([{"t": NOW - 6 * DAY, "placed": True, "entry_order": {"id": 1}, "side": "sell"},
                       {"t": NOW - DAY // 2, "event": "amend_stop", "placed": True},
                       {"t": NOW - 3600_000, "event": "closed_on_exchange", "delta_symbol": "ADAUSD"}], NOW)
    s = {"mode": "testnet", "live_orders": False, "kill_switch": False, "guard_deployed": True,
         "daily_loss_pct": 0.10, "min_wallet_inr": 350.0, "live_key_set": True, "families": "swing",
         "journal": j, "demo_open": 1, "stop_test": None, "hf": {"classifier_loaded": True, "last_seconds": 5},
         "models": [], "news_6h": 12, "news_sources_24h": {"coindesk": 5, "theblock": 3, "decrypt": 2, "x": 1},
         "news_future": 0, "live_wallet_inr": 500.0, "uptime_s": 7200}
    s.update(kw)
    return s


def _gate(r, name):
    return next(g for g in r["gates"] if g["name"].startswith(name))


def test_all_engineering_gates_met_leaves_only_the_owner_yes():
    r = evaluate(_snap())
    assert r["verdict"] == "ENGINEERING READY · OWNER STEPS LEFT"
    assert _gate(r, "REAL MONEY")["status"] == "wait"


def test_no_exchange_stop_fill_seen_is_not_ready():
    s = _snap()
    s["journal"]["exchange_closes"] = 0
    r = evaluate(s)
    assert r["verdict"] == "NOT READY" and _gate(r, "Delta's own stop")["status"] == "fail"


def test_stop_test_file_counts_as_seen():
    s = _snap(stop_test={"passed": True, "at": "2026-10-15"})
    s["journal"]["exchange_closes"] = 0
    assert _gate(evaluate(s), "Delta's own stop")["status"] == "ok"


def test_guard_missing_or_young_demo_or_errors_block():
    assert evaluate(_snap(guard_deployed=False))["verdict"] == "NOT READY"
    s = _snap(); s["journal"]["days_running"] = 1.5
    assert evaluate(s)["verdict"] == "NOT READY"
    s = _snap(); s["journal"]["errors_24h"] = 2
    assert evaluate(s)["verdict"] == "NOT READY"


def test_wallet_unknown_and_intraday_are_flagged():
    r = evaluate(_snap(live_wallet_inr=None, families="swing,intraday"))
    assert _gate(r, "Rs500 deposited")["status"] == "wait"
    assert next(c for c in r["checks"] if c["name"] == "Books opening paper trades")["status"] == "warn"


def test_journal_counts_errors_only_in_the_last_day():
    j = journal_stats([{"t": NOW - 2 * DAY, "error": "old"}, {"t": NOW - 60_000, "error": "delta 400: x"}], NOW)
    assert j["errors_24h"] == 1 and j["last_error"].startswith("delta 400")
