"""Fine-tuning pipeline: AI calls logged as training data, time-based split, Kaggle run wiring (no network)."""
import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_successful_ai_calls_are_logged_as_training_examples(tmp_path, monkeypatch):
    from collectors import llm_client
    from config.settings import settings
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "llm_call_log", True)

    async def fake(provider, model, *a, **k):
        return '{"score": 0.4}'

    monkeypatch.setattr(llm_client, "_call_openai_shaped", fake)
    monkeypatch.setattr(llm_client, "chain_for", lambda role: [("groq", "openai/gpt-oss-20b")])
    await llm_client.ask_json("news_scoring", "SYSTEM", "coindesk: ETF inflows")
    rec = json.loads(next((tmp_path / "data/llm_calls").glob("*.jsonl")).read_text().splitlines()[0])
    assert (rec["role"], rec["system"], rec["user"], rec["response"]) == ("news_scoring", "SYSTEM", "coindesk: ETF inflows", '{"score": 0.4}')


def test_split_holds_out_the_newest_examples():
    from scripts.export_finetune_data import split
    rows = [{"ts": f"2026-09-{d:02d}"} for d in range(1, 21)]
    train, test = split(rows)
    assert len(test) == 3 and min(r["ts"] for r in test) > max(r["ts"] for r in train)


@pytest.mark.asyncio
async def test_kaggle_start_uploads_data_and_pushes_a_private_gpu_kernel(monkeypatch):
    from scripts import kaggle_finetune as kf
    calls = []

    def fake_kaggle(args, keys, cwd=None):
        calls.append(args)
        if args[:2] == ["kernels", "push"]:
            meta = json.loads(open(args[3] + "/kernel-metadata.json").read())
            assert meta["enable_gpu"] and meta["enable_internet"] and meta["is_private"]
            assert meta["dataset_sources"] == ["dhruvdp/crypto-desk-sft"]
        return "ok"

    monkeypatch.setattr(kf, "_keys", AsyncMock(return_value={"user": "dhruvdp", "key": "k", "hf": "h"}))
    monkeypatch.setattr(kf, "_kaggle", fake_kaggle)
    rows = [{"job": "headline", "ts": f"2026-09-{d:02d}", "messages": []} for d in range(1, 21)]
    with patch("scripts.export_finetune_data.collect", AsyncMock(return_value={"headline": rows})):
        out = await kf.start_async()
    assert "20 examples uploaded" in out
    assert [c[:2] for c in calls] == [["datasets", "version"], ["kernels", "push"]]


@pytest.mark.asyncio
async def test_collect_uploads_only_a_model_that_beat_the_base(tmp_path, monkeypatch):
    from scripts import kaggle_finetune as kf
    monkeypatch.chdir(tmp_path)

    def fake_kaggle(args, keys, cwd=None):
        out = tmp_path / "data/finetune/run"
        out.mkdir(parents=True, exist_ok=True)
        (out / "eval.json").write_text(json.dumps({"better": False}))
        (out / "model-q4_k_m.gguf").write_text("x")
        return "ok"

    monkeypatch.setattr(kf, "_keys", AsyncMock(return_value={"user": "dhruvdp", "key": "k", "hf": "h"}))
    monkeypatch.setattr(kf, "_kaggle", fake_kaggle)
    res = await kf.collect_async()
    assert res["model_file"] and "uploaded_to" not in res
