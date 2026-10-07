"""Phase 2 (docs/INTEGRATION_PLAN.md 2.1, 2.2): honest permutation p-values, Bonferroni on N, and the trial ledger."""

import pandas as pd

from analysis.lab import ledger
from analysis.null_test import NullResult
from analysis.significance import judge, min_trials, perm_p


def test_p_value_is_never_zero():
    # old code: 0 beaten out of 200 reported p = 0.0
    assert perm_p(0, 200) == 1 / 201
    assert NullResult(10, 5.0, 0.0, 1.0, trials=200, beaten_by=0).p_value > 0
    assert perm_p(0, 0) == 1.0


def test_bonferroni_uses_how_many_configs_were_tried():
    # p = 0.01 passes alone, but not after 50 tries (line = 0.001)
    assert judge(0.01, n_tried=1) == "beats random"
    assert judge(0.01, n_tried=50) == "inconclusive"
    r = NullResult(10, 5.0, 0.0, 1.0, trials=1000, beaten_by=9, n_tried=50)
    assert r.verdict == "inconclusive"


def test_too_few_random_trials_cannot_pass():
    assert min_trials(10, 0.05) == 4000
    assert judge(1 / 501, n_tried=10, trials=500) == "too few trials"
    assert judge(0.9, n_tried=1) == "no better than random"


def _summary(cfgs):
    return pd.DataFrame([{"strategy": "donchian", "params": p, "exit": "sl3.0_rr3.0_h10080_beNone_trNone_pNone",
                          "cfg": f"donchian|{p}|x", "trades": 40, "expectancy_r": 0.2} for p in cfgs])


def test_ledger_counts_distinct_configs_and_ignores_reruns(tmp_path):
    path = tmp_path / "ledger.jsonl"
    fp = "data1"

    def rows(ps):
        return [{"digest": ledger.trial_digest("donchian", {"n": p}, "4h", "swing", (None, None), "india_gst", fp),
                 "cfg": p} for p in ps]

    assert ledger.record(rows([50, 100]), "swing", path=path)["n_family"] == 2
    res = ledger.record(rows([100, 150]), "swing", path=path)          # 100 is a rerun: one new
    assert (res["new"], res["n_family"]) == (1, 3)
    ledger.record(rows([200]), "swing", kind="stress", path=path)     # stress runs are not counted
    assert ledger.trial_count("swing", path=path) == 3
    assert ledger.trial_count("other", path=path) == 0


def test_digest_changes_when_data_or_params_change():
    a = ledger.trial_digest("donchian", {"n": 100}, "4h", "swing", (None, None), "india_gst", "data1")
    assert a != ledger.trial_digest("donchian", {"n": 101}, "4h", "swing", (None, None), "india_gst", "data1")
    assert a != ledger.trial_digest("donchian", {"n": 100}, "4h", "swing", (None, None), "india_gst", "data2")
    assert a == ledger.trial_digest("donchian", {"n": 100}, "4h", "swing", (None, None), "india_gst", "data1")


def test_lab_run_records_trials_and_uses_ledger_n(tmp_path, monkeypatch):
    from analysis.lab import runner
    monkeypatch.setattr(ledger, "LEDGER", tmp_path / "l.jsonl")
    spec = runner.RunSpec(strategies=["donchian"], ledger_family="t", root=str(tmp_path))
    assert runner._record_trials(spec, _summary(["n=50", "n=100"]), ["BTCUSDT"], log=lambda *_: None) == 2
    assert runner._record_trials(spec, _summary(["n=150"]), ["BTCUSDT"], log=lambda *_: None) == 3
    no_ledger = runner.RunSpec(strategies=["donchian"])
    assert runner._record_trials(no_ledger, _summary(["n=50"]), ["BTCUSDT"]) == 1
