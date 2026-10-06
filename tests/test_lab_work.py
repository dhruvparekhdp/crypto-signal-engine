"""The shared work queue: unique claims, model filters, stale recovery, completion."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from scripts import work_server as ws


def make(tmp, n_items=6, models=("m1", "m2")):
    d = Path(tmp) / "work" / "job"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({"name": "job", "models": list(models), "n_items": n_items,
                                              "system": "s", "schema": {}, "think_tokens": 10}))
    (d / "items.jsonl").write_text("\n".join(json.dumps({"i": i, "user": f"u{i}"}) for i in range(n_items)) + "\n")
    return d


class TestQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = ws.ROOT
        ws.ROOT = Path(self.tmp.name)
        self.d = make(self.tmp.name)

    def tearDown(self):
        ws.ROOT = self.old
        self.tmp.cleanup()

    def test_every_task_is_handed_out_exactly_once_under_concurrency(self):
        got, lock = [], threading.Lock()

        def worker(name):
            while True:
                c = ws.claim("job", name, ["m1", "m2"])
                if c["idx"] is None:
                    return
                with lock:
                    got.append(c["idx"])
        ts = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(got), list(range(12)))              # 6 items x 2 models, no duplicates, none lost

    def test_a_worker_only_gets_models_it_has(self):
        seen = set()
        while True:
            c = ws.claim("job", "small", ["m1"])
            if c["idx"] is None:
                break
            seen.add(c["model"])
        self.assertEqual(seen, {"m1"})
        self.assertFalse(c["all_done"])                              # m2 tasks still wait for a laptop that has m2

    def test_a_dead_workers_claim_is_given_to_someone_else(self):
        first = ws.claim("job", "ghost", ["m1", "m2"], now=1000.0)
        for _ in range(11):
            ws.claim("job", "other", ["m1", "m2"], now=1000.0)
        self.assertIsNone(ws.claim("job", "x", ["m1", "m2"], now=1500.0)["idx"])        # still fresh: not reclaimable
        again = ws.claim("job", "x", ["m1", "m2"], now=1000.0 + ws.STALE_S + 1)
        self.assertEqual(again["idx"], first["idx"])

    def test_finished_tasks_are_not_reissued_and_all_done_is_reported(self):
        for _ in range(12):
            c = ws.claim("job", "w", ["m1", "m2"])
            ws.result("job", {"idx": c["idx"], "ok": True})
        end = ws.claim("job", "w", ["m1", "m2"])
        self.assertIsNone(end["idx"])
        self.assertTrue(end["all_done"])
        self.assertEqual(ws.status("job")["done"], 12)

    def test_task_numbering_matches_item_and_model(self):
        c = ws.claim("job", "w", ["m1", "m2"])
        self.assertEqual((c["idx"], c["item"], c["model"]), (0, 0, "m1"))
        c = ws.claim("job", "w", ["m1", "m2"])
        self.assertEqual((c["idx"], c["item"], c["model"]), (1, 0, "m2"))


class TestWorkerParsing(unittest.TestCase):
    def test_worker_is_standard_library_only(self):
        import ast
        tree = ast.parse(Path("scripts/work_worker.py").read_text())
        mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertTrue(mods <= {"__future__", "argparse", "json", "subprocess", "sys", "time", "urllib", "os"}, mods)

    def test_json_is_extracted_from_text_around_it(self):
        from scripts.work_worker import json_from
        self.assertEqual(json_from('thinking... {"decision": "take", "score": 3} done'), {"decision": "take", "score": 3})


if __name__ == "__main__":
    unittest.main()


class TestBaseline(unittest.TestCase):
    def test_baseline_never_trains_on_the_trades_it_scores(self):
        import numpy as np
        from analysis.lab_ai import baseline
        rng = np.random.default_rng(0)
        rows = [{"entry_t": i, "r_net": float(rng.normal()), "features": {k: float(rng.normal()) for k in baseline.KEYS}}
                for i in range(400)]
        scored = baseline.walk_forward(rows, min_train=120, block=40)
        self.assertEqual(min(r["entry_t"] for r in scored), 120)         # nothing before 120 is scored: it is only training data
        self.assertEqual(len(scored), 280)

    def test_baseline_finds_a_real_signal_and_ignores_noise(self):
        import numpy as np
        from analysis.lab_ai import baseline
        rng = np.random.default_rng(1)
        rows = []
        for i in range(600):
            f = {k: float(rng.normal()) for k in baseline.KEYS}
            rows.append({"entry_t": i, "r_net": float(0.8 * f["vol"] + rng.normal(0, 1)), "features": f})
        s = baseline.summarize(baseline.walk_forward(rows))
        self.assertGreater(s["lift_r_per_trade"], 0.3)
        noise = [{"entry_t": i, "r_net": float(rng.normal()), "features": {k: float(rng.normal()) for k in baseline.KEYS}} for i in range(600)]
        self.assertLess(abs(baseline.summarize(baseline.walk_forward(noise))["lift_r_per_trade"]), 0.25)


class TestThinkingSampling(unittest.TestCase):
    def test_thinking_calls_never_use_near_greedy_temperature(self):
        from unittest import mock
        from analysis.lab_ai import ollama
        seen = []
        def fake(model, system, user, **kw):
            seen.append(kw)
            return {"text": "{}", "thinking": "", "out_tokens": 1}
        with mock.patch.object(ollama, "chat", side_effect=fake):
            ollama.chat_think("m", "s", "u", num_predict=10)
        self.assertGreaterEqual(seen[0]["temperature"], 0.5)
        self.assertGreater(seen[0]["repeat_penalty"], 1.0)

    def test_a_loop_abort_is_retried_once_with_looser_sampling(self):
        from unittest import mock
        from analysis.lab_ai import ollama
        calls = []
        def fake(model, system, user, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise ollama.OllamaError('500: {"error":"prediction aborted, token repeat limit reached"}')
            return {"text": "{}", "thinking": "", "out_tokens": 1}
        with mock.patch.object(ollama, "chat", side_effect=fake):
            ollama.chat_think("m", "s", "u", num_predict=10)
        self.assertEqual(len(calls), 2)
        self.assertGreater(calls[1]["repeat_penalty"], calls[0]["repeat_penalty"])

    def test_worker_uses_the_same_thinking_sampling(self):
        from scripts import work_worker
        self.assertEqual(work_worker.THINK["temperature"], 0.6)
        self.assertGreater(work_worker.THINK["repeat_penalty"], 1.0)


class TestR1Policy(unittest.TestCase):
    def test_r1_gets_a_bigger_budget_and_a_plain_text_followup(self):
        from unittest import mock
        from scripts import work_worker as w
        calls = []
        def fake_chat(model, system, user, **kw):
            calls.append(kw)
            return {"text": "", "thinking": "reasoning " * 50, "out_tokens": 5} if len(calls) == 1 else \
                   {"text": '{"decision": "skip", "score": 2, "reason": "x 1 2"}', "thinking": "", "out_tokens": 5}
        meta = {"system": "s", "schema": {"type": "object"}, "think_tokens": 700}
        with mock.patch.object(w, "chat", side_effect=fake_chat):
            r = w.decide(meta, {"user": "u"}, "deepseek-r1:8b")
        self.assertTrue(r["ok"])
        self.assertEqual(calls[0]["num_predict"], 1050)              # 1.5x the budget: r1 cannot stop thinking early
        self.assertIsNone(calls[1].get("schema"))                    # and the follow-up is plain text, not schema-constrained
        self.assertIsNone(calls[1].get("think"))

    def test_qwen_keeps_the_schema_constrained_answer_with_thinking_off(self):
        from unittest import mock
        from scripts import work_worker as w
        calls = []
        def fake_chat(model, system, user, **kw):
            calls.append(kw)
            return {"text": "", "thinking": "t", "out_tokens": 5} if len(calls) == 1 else \
                   {"text": '{"decision": "take", "score": 4, "reason": "a 1 b 2"}', "thinking": "", "out_tokens": 5}
        meta = {"system": "s", "schema": {"type": "object"}, "think_tokens": 700}
        with mock.patch.object(w, "chat", side_effect=fake_chat):
            r = w.decide(meta, {"user": "u"}, "qwen3:8b")
        self.assertTrue(r["ok"])
        self.assertEqual(calls[0]["num_predict"], 700)
        self.assertEqual(calls[1]["think"], False)
        self.assertEqual(calls[1]["schema"], {"type": "object"})


class TestRetries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = ws.ROOT
        ws.ROOT = Path(self.tmp.name)
        make(self.tmp.name, n_items=1, models=("m1",))

    def tearDown(self):
        ws.ROOT = self.old
        self.tmp.cleanup()

    def test_a_failed_task_is_offered_again_once_then_recorded_as_failed(self):
        c = ws.claim("job", "w", ["m1"])
        ws.result("job", {"idx": c["idx"], "ok": False})
        again = ws.claim("job", "w", ["m1"])
        self.assertEqual(again["idx"], c["idx"])                      # first failure: retried
        ws.result("job", {"idx": again["idx"], "ok": False})
        end = ws.claim("job", "w", ["m1"])
        self.assertIsNone(end["idx"])                                 # second failure: final
        self.assertTrue(end["all_done"])
        self.assertEqual(ws.status("job")["done"], 1)

    def test_a_good_answer_after_a_failure_wins(self):
        c = ws.claim("job", "w", ["m1"])
        ws.result("job", {"idx": c["idx"], "ok": False})
        c = ws.claim("job", "w", ["m1"])
        ws.result("job", {"idx": c["idx"], "ok": True, "decision": "take", "score": 3})
        self.assertTrue(ws.claim("job", "w", ["m1"])["all_done"])


def test_ai_gate_context_hides_coin_and_date_so_the_model_cannot_recall_history():
    import pandas as pd
    from unittest.mock import patch

    from analysis.lab_ai import gate
    fake = {"symbol": "SOLUSDT", "entry_utc": "2022-11-08 12:00", "side": "LONG", "stop_pct": 4.0,
            "at_entry": {"ret_1h_pct": 1.0}, "outcome": {"r_net": 3.0}, "next_16_closes_15m_pct_in_trade_direction": [1]}
    with patch("analysis.lab_ai.context.build", lambda *a, **k: dict(fake)):
        ctx = gate.pretrade_context(pd.Series({}))
        raw = gate.pretrade_context(pd.Series({}), anonymize=False)
    text = str(ctx)
    assert "SOL" not in text and "2022" not in text and ctx["coin"] == "Coin A"
    assert "outcome" not in ctx and "next_16_closes_15m_pct_in_trade_direction" not in ctx
    assert raw["symbol"] == "SOLUSDT" and "outcome" not in raw
