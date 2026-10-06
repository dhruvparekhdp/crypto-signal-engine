"""Training data for fine-tuning a small model on our own desk jobs (finetune/README.md).

Reads the production database (read-only) and writes chat-format JSONL: every example is the exact system prompt
production uses, the input it was given, and the answer a large model (Groq gpt-oss / qwen) gave. A small model
trained on these learns our three jobs:

  headline   news_sentiment     headline + source  -> {"score", "confidence", "event_type", "symbol"}
  attribution move_attributions moves + signals    -> why each coin moved (the stored JSON result)
  event      market_events      title + notes      -> {"category", "level", "direction"}

Split by time, not at random: the newest 15% of each job is held out, so the test asks "does it work on what came
after the training data", the question that matters. Also includes data/llm_calls/*.jsonl (every AI call logged
since 6 Oct) when present.

    python -m scripts.export_finetune_data --out data/finetune --push dhruvdp/crypto-desk-sft
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

VAL_SHARE = 0.15


MOVE_FIELDS = ("symbol", "price", "change_1h", "change_4h", "change_12h", "range_12h")


def chat(system: str, user: str, answer, job: str, ts: str) -> dict:
    a = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False, separators=(",", ":"))
    return {"job": job, "ts": ts, "messages": [{"role": "system", "content": system},
                                               {"role": "user", "content": user},
                                               {"role": "assistant", "content": a}]}


async def collect() -> dict[str, list[dict]]:
    from sqlalchemy import select

    from analysis.move_attribution import ATTRIBUTION_SYSTEM, Move, build_prompt
    from storage.database import AsyncSessionFactory
    from storage.models import MarketBriefing, MarketEvent, MoveAttribution, NewsSentiment

    out: dict[str, list[dict]] = {"headline": [], "attribution": [], "event": []}
    headline_system = _headline_system()
    async with AsyncSessionFactory() as s:
        for r in (await s.execute(select(NewsSentiment).order_by(NewsSentiment.received_at))).scalars():
            if not r.headline or not r.model or r.model.startswith("hf/finbert"):
                continue                        # only answers from a large model are worth imitating
            out["headline"].append(chat(headline_system, f"{r.source}: {r.headline}",
                                        {"score": round(r.score, 2), "confidence": round(r.confidence, 2),
                                         "event_type": r.event_type, "symbol": r.symbol},
                                        "headline", str(r.received_at)))
        briefings = {b.id: b for b in (await s.execute(select(MarketBriefing))).scalars()}
        skipped = 0
        for r in (await s.execute(select(MoveAttribution).order_by(MoveAttribution.created_at))).scalars():
            try:
                raw = json.loads(r.moves) if isinstance(r.moves, str) else (r.moves or [])
                if isinstance(raw, dict):                      # some rows store {symbol: {...}}
                    raw = [{"symbol": k, **v} for k, v in raw.items()]
                moves = [Move(**{k: m.get(k) for k in MOVE_FIELDS}) for m in raw if isinstance(m, dict)]
                sig_raw = json.loads(r.signals) if isinstance(r.signals, str) else (r.signals or [])
                signals = [{"at": x.get("at", ""), "symbol": x.get("symbol", ""), "direction": x.get("direction", ""),
                            "type": x.get("type", ""), "confidence": float(x.get("confidence") or 0),
                            "outcome": x.get("outcome", ""), "pnl_pct": float(x.get("pnl_pct") or 0),
                            "blocked_by": x.get("blocked_by", "")} for x in sig_raw if isinstance(x, dict)]
                result = json.loads(r.result) if isinstance(r.result, str) else r.result
            except (TypeError, ValueError, AttributeError):
                skipped += 1
                continue
            if not moves or not result:
                continue
            b = briefings.get(r.briefing_id)
            news = b.summary if b is not None else ""
            out["attribution"].append(chat(ATTRIBUTION_SYSTEM, build_prompt(moves, signals, news, r.window_hours or 12),
                                           result, "attribution", str(r.created_at)))
        if skipped:
            print(f"attribution rows skipped (unreadable): {skipped}")
        for r in (await s.execute(select(MarketEvent).order_by(MarketEvent.first_seen))).scalars():
            if not r.title:
                continue
            out["event"].append(chat(EVENT_SYSTEM, f"{r.title}\n{r.notes or ''}".strip(),
                                     {"category": r.category, "level": r.level_initial, "direction": r.direction},
                                     "event", str(r.first_seen)))
    for f in sorted(Path("data/llm_calls").glob("*.jsonl")) if Path("data/llm_calls").exists() else []:
        for line in f.read_text().splitlines():
            try:
                c = json.loads(line)
            except ValueError:
                continue
            if c.get("ok") and c.get("response"):
                out.setdefault(f"call_{c['role']}", []).append(chat(c["system"], c["user"], c["response"],
                                                                    f"call_{c['role']}", c.get("ts", "")))
    return out


EVENT_SYSTEM = ("Grade a market event for a crypto trading desk. JSON only: "
                '{"category": "<central_bank|inflation|jobs|geopolitics_war|sanctions|trade_tariffs|crypto_regulation|'
                'crypto_etf_flows|liquidations_funding|exchange_hack_insolvency|stablecoin|corporate_treasury|other>", '
                '"level": <1-5, how much it can move crypto>, "direction": "<up|down|unclear>"}')


def _headline_system() -> str:
    """The production prompt, built the same way scheduler/runner.py builds it."""
    from collectors.hermes import EVENT_TYPES
    return ("Score a news headline for a crypto trading desk. One headline, one JSON object, nothing else.\n\n"
            "score: -1.0 to 1.0, how this moves risk assets. A rate HIKE is negative. A rate CUT is positive. War and "
            "tariffs are negative. ETF inflows and adoption are positive. An exchange hack is negative.\n\n"
            "confidence: 0.0 to 1.0. Be honest. A vague headline about 'experts predicting' deserves 0.1, a stated Fed "
            "decision deserves 0.9.\n\nMost headlines are noise. Price-prediction pieces, opinion, 'what to watch', "
            "anything about a token nobody trades — score those 0.0 with event_type 'noise'. A scorer that finds "
            "meaning in everything is a scorer nobody can act on.\n\n"
            f"event_type, use only these: {', '.join(EVENT_TYPES)}\n\n"
            "symbol: which coin this is about, or 'all' if it affects the whole market.\n\n"
            'JSON only: {"score": 0.0, "confidence": 0.0, "event_type": "noise", "symbol": "all"}')


def split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    rows = sorted(rows, key=lambda r: r["ts"])
    k = max(1, int(len(rows) * VAL_SHARE)) if len(rows) >= 7 else 0
    return rows[: len(rows) - k], rows[len(rows) - k:]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/finetune")
    ap.add_argument("--push", default="", help="private Hugging Face dataset repo to upload to, e.g. dhruvdp/crypto-desk-sft")
    a = ap.parse_args()
    jobs = asyncio.run(collect())
    train, val = [], []
    for job, rows in jobs.items():
        t, v = split(rows)
        train += t
        val += v
        print(f"{job:18} {len(rows):5} examples -> train {len(t):5}  test {len(v):4}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("train", train), ("test", val)):
        with open(out / f"{name}.jsonl", "w") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"saved {len(train)} train / {len(val)} test to {out}")
    if a.push:
        asyncio.run(_push(out, a.push))


async def _push(folder: Path, repo: str):
    from huggingface_hub import HfApi

    from config.overrides import apply
    from config.settings import settings
    from scheduler.keys_page import _stored
    apply(settings, await _stored())
    api = HfApi(token=settings.hf_api_token.get_secret_value())
    api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
    api.upload_folder(folder_path=str(folder), repo_id=repo, repo_type="dataset", allow_patterns=["*.jsonl"],
                      commit_message="training data export")
    print("uploaded to private dataset", repo)


if __name__ == "__main__":
    main()
