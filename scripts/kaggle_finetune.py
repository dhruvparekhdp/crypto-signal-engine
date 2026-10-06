"""Run the fine-tune on Kaggle's free GPU, end to end, with the Kaggle and Hugging Face keys saved on /keys.

    python -m scripts.kaggle_finetune start     # export data, upload it, push the training kernel (GPU on)
    python -m scripts.kaggle_finetune status    # where the run is
    python -m scripts.kaggle_finetune collect   # download model + eval; upload the model to a private HF repo
                                                #   only if it beat the base model on the held-out test

Nothing changes in production here: switching the Space to the new model is a separate, deliberate step
(finetune/README.md). Kaggle needs a phone-verified account for GPU and internet in kernels.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DATASET_SLUG = "crypto-desk-sft"
KERNEL_SLUG = "crypto-desk-finetune"
MODEL_REPO_NAME = "crypto-analyst-3b-gguf"


async def _keys() -> dict:
    from config.overrides import apply
    from config.settings import settings
    from scheduler.keys_page import _stored
    apply(settings, await _stored())
    user = (settings.kaggle_username or "").strip()
    key = settings.kaggle_key.get_secret_value() if settings.kaggle_key else ""
    if not user or not key:
        raise SystemExit("save 'Kaggle username' and 'Kaggle API key' on /keys first")
    hf = settings.hf_api_token.get_secret_value() if settings.hf_api_token else ""
    return {"user": user, "key": key, "hf": hf}


def _kaggle(args: list[str], keys: dict, cwd: str | None = None) -> str:
    env = {**os.environ, "KAGGLE_USERNAME": keys["user"]}
    k = keys["key"]
    if len(k) == 32 and all(c in "0123456789abcdef" for c in k):
        env["KAGGLE_KEY"] = k                     # legacy kaggle.json key
    else:
        env["KAGGLE_API_TOKEN"] = k               # new-style API token (Kaggle CLI 2.x needs it for uploads)
    r = subprocess.run([sys.executable, "-m", "kaggle", *args], env=env, cwd=cwd, capture_output=True, text=True,
                       timeout=600)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        raise RuntimeError(f"kaggle {' '.join(args[:2])} failed: {out[-300:]}")
    return out


async def start_async() -> str:
    from scripts import export_finetune_data as ex
    keys = await _keys()
    work = Path(tempfile.mkdtemp())
    data = work / "data"
    data.mkdir()
    jobs = await ex.collect()
    n = 0
    for name in ("train", "test"):
        with open(data / f"{name}.jsonl", "w") as f:
            for job, rows in jobs.items():
                t, v = ex.split(rows)
                for r in (t if name == "train" else v):
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    n += 1
    (data / "dataset-metadata.json").write_text(json.dumps(
        {"title": "crypto desk sft", "id": f"{keys['user']}/{DATASET_SLUG}", "licenses": [{"name": "other"}]}))
    try:
        _kaggle(["datasets", "version", "-p", str(data), "-m", "refresh", "-r", "zip"], keys)
    except RuntimeError:
        _kaggle(["datasets", "create", "-p", str(data), "-r", "zip"], keys)       # private by default
    kern = work / "kernel"
    kern.mkdir()
    shutil.copy(Path(__file__).resolve().parent.parent / "finetune" / "finetune_qwen.py", kern / "finetune_qwen.py")
    (kern / "kernel-metadata.json").write_text(json.dumps({
        "id": f"{keys['user']}/{KERNEL_SLUG}", "title": KERNEL_SLUG, "code_file": "finetune_qwen.py",
        "language": "python", "kernel_type": "script", "is_private": True, "enable_gpu": True,
        "enable_internet": True, "dataset_sources": [f"{keys['user']}/{DATASET_SLUG}"], "competition_sources": [],
        "kernel_sources": []}))
    out = _kaggle(["kernels", "push", "-p", str(kern)], keys)
    return f"{n} examples uploaded; training started: {out[-200:]}"


async def status_async() -> str:
    keys = await _keys()
    return _kaggle(["kernels", "status", f"{keys['user']}/{KERNEL_SLUG}"], keys)


async def collect_async() -> dict:
    keys = await _keys()
    out = Path("data/finetune/run")
    out.mkdir(parents=True, exist_ok=True)
    _kaggle(["kernels", "output", f"{keys['user']}/{KERNEL_SLUG}", "-p", str(out), "-o"], keys)
    ev = json.loads((out / "eval.json").read_text()) if (out / "eval.json").exists() else {}
    res = {"eval": ev, "model_file": (out / "model-q4_k_m.gguf").exists()}
    if ev.get("better") and res["model_file"] and keys["hf"]:
        from huggingface_hub import HfApi
        api = HfApi(token=keys["hf"])
        who = api.whoami()["name"]
        repo = f"{who}/{MODEL_REPO_NAME}"
        api.create_repo(repo, private=True, exist_ok=True)
        api.upload_file(path_or_fileobj=str(out / "model-q4_k_m.gguf"), path_in_repo="model-q4_k_m.gguf", repo_id=repo)
        api.upload_file(path_or_fileobj=str(out / "eval.json"), path_in_repo="eval.json", repo_id=repo)
        res["uploaded_to"] = repo
    return res


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    fn = {"start": start_async, "status": status_async, "collect": collect_async}[cmd]
    print(asyncio.run(fn()))
