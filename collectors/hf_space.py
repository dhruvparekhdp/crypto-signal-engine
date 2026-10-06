"""Client for our own Hugging Face Space's fast endpoints (huggingface_space/app.py): headline classifier and
embeddings. One HTTP call per batch, under the shared AI budget (collectors/llm_budget.py, key "hf/classify")."""
from __future__ import annotations

import httpx

from config.settings import settings


def configured() -> bool:
    return bool((settings.hf_base_url or "").strip())


def _headers() -> dict:
    key = settings.hf_space_api_key or settings.hf_api_token
    v = key.get_secret_value() if key is not None else ""
    return {"Content-Type": "application/json", **({"Authorization": f"Bearer {v}"} if v else {})}


async def _post(path: str, texts: list[str], timeout: float) -> dict:
    from collectors.llm_budget import budget
    ok, wait, why = budget().allow("hf", "classify", est_tokens=0)
    if not ok:
        raise RuntimeError(f"hf space budget: {why}")
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(settings.hf_base_url.rstrip("/") + path, json={"texts": texts}, headers=_headers())
    if r.status_code != 200:
        if r.status_code in (429, 503):
            budget().cooldown_from_error("hf", "classify", r.text, r.headers.get("retry-after"))
        raise RuntimeError(f"hf space {path} returned {r.status_code}: {r.text[:160]}")
    budget().observe("hf", "classify", 0)
    return r.json()


async def classify(texts: list[str], timeout: float = 120.0) -> list[dict]:
    """[{positive, negative, neutral}] per text. The first call after the Space starts loads the model (~1 min)."""
    if not texts:
        return []
    rows = (await _post("/v1/classify", texts, timeout)).get("results", [])
    if len(rows) != len(texts):
        raise RuntimeError(f"hf space classify returned {len(rows)} rows for {len(texts)} texts")
    return rows


async def embed(texts: list[str], timeout: float = 60.0) -> list[list[float]]:
    return (await _post("/v1/embed", texts, timeout)).get("vectors", []) if texts else []
