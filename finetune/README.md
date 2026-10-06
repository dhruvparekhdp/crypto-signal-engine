# Fine-tuning our own small model (Kaggle free GPU)

Goal: one small model (Qwen2.5-3B-Instruct, LoRA) that does our desk's AI jobs, running free on the Hugging Face
Space's CPU, so production depends less on rate-limited free APIs.

## Data (scripts/export_finetune_data.py)
Chat-format examples: the exact production system prompt, the input, and the answer a large model gave.

| Job | Source table | Examples (6 Oct) | Answer |
|---|---|---|---|
| headline | news_sentiment | 764 | score, confidence, event_type, symbol |
| attribution | move_attributions | 176 | why each coin moved (JSON) |
| event | market_events | 341 | category, level 1-5, direction |
| call_* | data/llm_calls/*.jsonl | grows daily | every successful AI call since 6 Oct |

Split by time: the newest 15% of each job is the test set (does it work on what came after?).
Not usable yet: market_briefings and signal_reviews saved only answers, not inputs; the call log fixes that.

## Run (keys on /keys: Kaggle username, Kaggle API key, Hugging Face token)
```
python -m scripts.kaggle_finetune start     # export, upload private dataset, push GPU kernel
python -m scripts.kaggle_finetune status
python -m scripts.kaggle_finetune collect   # download eval + model; upload to private HF repo only if better
```
The kernel (finetune/finetune_qwen.py) scores the base model on the test set first, trains 3 epochs on answers only,
scores again, writes `eval.json` and `model-q4_k_m.gguf`. Kaggle accounts need phone verification for GPU/internet.

## Switching production (deliberate step)
Only if `eval.json` says better: set the Space variables MODEL_REPO=<user>/crypto-analyst-3b-gguf and
MODEL_FILE=model-q4_k_m.gguf (Space settings), restart the Space, then watch /api/llm/budget and the
health card for a few days. Roll back by clearing the two variables.

## Next rounds
Retrain monthly as data/llm_calls grows; add briefing and pre-trade review jobs once their inputs are logged;
consider a 7B model only if the Space's speed allows it.
