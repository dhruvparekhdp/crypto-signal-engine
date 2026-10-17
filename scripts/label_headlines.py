"""Label every stored headline with the bake-off's best noise filter (Groq gpt-oss-120b: 100% of known noise caught,
15 Oct 2026) - the training set for a tiny triage model fine-tuned on Kaggle's free GPU (owner, 18 Oct 2026).

    PYTHONPATH=. python -m scripts.label_headlines            # on the server (keys + database live there)

Writes data/finetune/triage_labels.jsonl, one headline per line: {"headline", "source", "published_at", "relevant",
"score", "confidence", "event_type", "labeler"}. Resumable: headlines already labelled are skipped. Batches of 30,
spaced for Groq's 8k tokens/minute so production keeps its share.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

OUT = Path("data/finetune/triage_labels.jsonl")
MODEL = ("groq", "openai/gpt-oss-120b")
BATCH, PACE_S = 30, 45


async def main() -> None:
    from sqlalchemy import select

    from config.overrides import apply
    from config.settings import settings
    from scheduler.keys_page import _stored
    apply(settings, await _stored())
    from collectors.llm_client import PROVIDERS, _call_openai_shaped, _extract_json
    from scripts.ai_bakeoff import NEWS_SYSTEM
    from storage.database import AsyncSessionFactory
    from storage.models import NewsSentiment as N

    async with AsyncSessionFactory() as s:
        rows = (await s.execute(select(N.headline, N.source, N.published_at).order_by(N.published_at))).all()
    seen = set()
    if OUT.exists():
        for ln in OUT.read_text().splitlines():
            try:
                seen.add(json.loads(ln)["headline"])
            except (ValueError, KeyError):
                pass
    todo, dup = [], set()
    for h, src, pub in rows:
        if not h or h in seen or h in dup or (src or "").startswith("web_briefing"):
            continue
        dup.add(h)
        todo.append({"headline": h, "source": (src or "").split(":")[0], "published_at": str(pub)[:16]})
    print(f"{len(rows)} stored, {len(seen)} already labelled, {len(todo)} to label", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    provider = PROVIDERS[MODEL[0]]
    done = 0
    with OUT.open("a") as f:
        for b in range(0, len(todo), BATCH):
            batch = todo[b:b + BATCH]
            user = "\n".join(f"{k + 1}. [{x['source']}, {x['published_at']} UTC] {x['headline']}" for k, x in enumerate(batch))
            try:
                text = await _call_openai_shaped(provider, MODEL[1], NEWS_SYSTEM, user, 80 * len(batch), 0.0, 120)
                items = (_extract_json(text) or {}).get("items") or []
            except Exception as e:  # noqa: BLE001 - skip this batch, keep going (resumable)
                print("batch failed:", str(e)[:120], flush=True)
                items = []
            by_i = {int(x.get("i", 0)): x for x in items if isinstance(x, dict) and str(x.get("i", "")).isdigit()}
            for k, x in enumerate(batch):
                y = by_i.get(k + 1)
                if not y:
                    continue
                f.write(json.dumps({**x, "relevant": bool(y.get("relevant")), "score": y.get("score"),
                                    "confidence": y.get("confidence"), "event_type": y.get("event_type"),
                                    "labeler": "/".join(MODEL)}) + "\n")
                done += 1
            f.flush()
            print(f"{min(b + BATCH, len(todo))}/{len(todo)} labelled {done}", flush=True)
            await asyncio.sleep(PACE_S)
    print("done", done, "at", time.strftime("%H:%M UTC", time.gmtime()), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
