"""
Fine-tune Qwen2.5-3B-Instruct on our desk jobs with LoRA, on Kaggle's free GPU (T4 x2 / P100). Runs as a Kaggle
script kernel pushed by scripts/kaggle_finetune.py; reads the dataset that script uploads and writes everything to
/kaggle/working, which the server then downloads:

    model-q4_k_m.gguf   the fine-tuned model, 4-bit, ready for the Hugging Face Space (llama.cpp)
    eval.json           base model vs fine-tuned model on the held-out (newest) examples
    train_log.json      loss per step

The base model is scored on the same test set first, so "is it better" is a measured answer, not a hope.
"""
import json
import os
import re
import subprocess
import sys
import time

subprocess.run([sys.executable, "-m", "pip", "install", "-q", "unsloth"], check=True)

import torch  # noqa: E402
from datasets import load_dataset  # noqa: E402
from unsloth import FastLanguageModel  # noqa: E402
from unsloth.chat_templates import train_on_responses_only  # noqa: E402
from trl import SFTConfig, SFTTrainer  # noqa: E402

BASE = os.environ.get("BASE_MODEL", "unsloth/Qwen2.5-3B-Instruct-bnb-4bit")
MAX_LEN = 4096
OUT = "/kaggle/working"
DATA = next(os.path.join(r, f) for r, _, fs in os.walk("/kaggle/input") for f in fs if f == "train.jsonl").rsplit("/", 1)[0]
print("data:", DATA, "| gpu:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none", flush=True)

ds = load_dataset("json", data_files={"train": f"{DATA}/train.jsonl", "test": f"{DATA}/test.jsonl"})
model, tok = FastLanguageModel.from_pretrained(BASE, max_seq_length=MAX_LEN, load_in_4bit=True)


def to_text(ex):
    return {"text": tok.apply_chat_template(ex["messages"], tokenize=False)}


def first_json(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def evaluate(label: str, limit: int = 120) -> dict:
    """JSON validity for every job; for headlines also score error and event-type accuracy."""
    FastLanguageModel.for_inference(model)
    rows = list(ds["test"])[:limit]
    valid, err, ev_ok, ev_n, by_job = 0, [], 0, 0, {}
    t0 = time.time()
    for ex in rows:
        prompt = tok.apply_chat_template(ex["messages"][:2], tokenize=False, add_generation_prompt=True)
        ids = tok(prompt, return_tensors="pt").to("cuda")
        out = model.generate(**ids, max_new_tokens=600, do_sample=False)
        got = first_json(tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=True))
        want = json.loads(ex["messages"][2]["content"]) if ex["messages"][2]["content"].startswith("{") else None
        j = by_job.setdefault(ex["job"], {"n": 0, "valid": 0})
        j["n"] += 1
        if got is not None:
            valid += 1
            j["valid"] += 1
        if ex["job"] == "headline" and got is not None and want is not None:
            try:
                err.append(abs(float(got.get("score", 0)) - float(want["score"])))
            except (TypeError, ValueError):
                pass
            ev_n += 1
            ev_ok += int(got.get("event_type") == want.get("event_type"))
    res = {"model": label, "examples": len(rows), "json_valid": valid / max(1, len(rows)),
           "headline_score_mae": sum(err) / len(err) if err else None,
           "headline_event_accuracy": ev_ok / ev_n if ev_n else None, "by_job": by_job,
           "seconds": round(time.time() - t0)}
    print(json.dumps(res), flush=True)
    return res


base_eval = evaluate("base " + BASE)

model = FastLanguageModel.get_peft_model(
    model, r=16, lora_alpha=16, lora_dropout=0, bias="none", use_gradient_checkpointing="unsloth",
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"], random_state=11)
train = ds["train"].map(to_text)
trainer = SFTTrainer(model=model, tokenizer=tok, train_dataset=train, args=SFTConfig(
    dataset_text_field="text", max_seq_length=MAX_LEN, per_device_train_batch_size=2, gradient_accumulation_steps=4,
    num_train_epochs=3, learning_rate=2e-4, warmup_steps=10, logging_steps=5, lr_scheduler_type="cosine",
    fp16=not torch.cuda.is_bf16_supported(), bf16=torch.cuda.is_bf16_supported(), output_dir=f"{OUT}/ckpt",
    save_strategy="no", report_to="none", seed=11))
# learn only the answers, not to repeat the prompts
trainer = train_on_responses_only(trainer, instruction_part="<|im_start|>user\n", response_part="<|im_start|>assistant\n")
trainer.train()
json.dump(trainer.state.log_history, open(f"{OUT}/train_log.json", "w"))

tuned_eval = evaluate("fine-tuned")
json.dump({"base": base_eval, "tuned": tuned_eval, "better": tuned_eval["json_valid"] >= base_eval["json_valid"] and (
    tuned_eval["headline_score_mae"] or 9) <= (base_eval["headline_score_mae"] or 9)}, open(f"{OUT}/eval.json", "w"), indent=1)

model.save_pretrained_gguf(f"{OUT}/gguf", tok, quantization_method="q4_k_m")
# Unsloth names the folder and file itself (e.g. gguf_gguf/Qwen2.5-3B-Instruct.Q4_K_M.gguf): find it anywhere
found = [os.path.join(r, f) for r, _, fs in os.walk(OUT) for f in fs if f.endswith(".gguf") and "Q4_K_M" in f.upper()]
if found:
    os.replace(found[0], f"{OUT}/model-q4_k_m.gguf")
subprocess.run(["bash", "-c", f"rm -rf {OUT}/gguf* {OUT}/ckpt {OUT}/unsloth_compiled_cache"])
print("done", os.listdir(OUT), flush=True)
