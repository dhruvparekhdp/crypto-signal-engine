"""
Crypto analyst LLM that runs ON the Space's own CPU (free tier: 2 vCPU, 16 GB RAM) with llama.cpp.

No inference credits and no daily token limit: the model file is downloaded once at start-up and served from
memory. It is slow (roughly 1-3 minutes per answer on 2 vCPU), so the trading engine uses it as the LAST fallback
for its search roles (market briefing, move attribution), after Groq and OpenRouter.

API (OpenAI-shaped, what collectors/llm_client.py's "hf" provider calls via HF_BASE_URL):
    POST /v1/chat/completions   {"model": "analyst+search", "messages": [...], "max_tokens": 600}
    GET  /health
A model name ending in "+search" or ":online" first fetches DuckDuckGo results and puts them in the prompt.

Space settings (Settings -> Variables and secrets):
    SPACE_API_KEY  (secret)  callers must send  Authorization: Bearer <SPACE_API_KEY>
    MODEL_REPO     (optional) default Qwen/Qwen2.5-3B-Instruct-GGUF
    MODEL_FILE     (optional) default qwen2.5-3b-instruct-q4_k_m.gguf
"""
from __future__ import annotations

import asyncio
import os
import re
import threading
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

MODEL_REPO = os.getenv("MODEL_REPO", "Qwen/Qwen2.5-3B-Instruct-GGUF")
MODEL_FILE = os.getenv("MODEL_FILE", "qwen2.5-3b-instruct-q4_k_m.gguf")
API_KEY = os.getenv("SPACE_API_KEY", "")
CTX = int(os.getenv("CTX", "4096"))

app = FastAPI(title="Crypto Analyst LLM (CPU)")
_llm = None
_lock = threading.Lock()          # llama.cpp is not re-entrant: one generation at a time
_stats = {"started": time.time(), "served": 0, "last_seconds": None, "loading": True, "error": None}


def _load():
    global _llm
    try:
        from huggingface_hub import hf_hub_download
        from llama_cpp import Llama
        path = hf_hub_download(MODEL_REPO, MODEL_FILE)
        _llm = Llama(model_path=path, n_ctx=CTX, n_threads=os.cpu_count() or 2, verbose=False)
    except Exception as e:  # noqa: BLE001
        _stats["error"] = str(e)[:300]
    finally:
        _stats["loading"] = False


threading.Thread(target=_load, daemon=True).start()


def _search(query: str, n: int = 5) -> str:
    try:
        from duckduckgo_search import DDGS
        with DDGS() as d:
            rows = list(d.news(query, max_results=n)) or list(d.text(query, max_results=n))
        return "\n".join(f"- {r.get('title', '')}: {r.get('body', '')[:240]} ({r.get('date', '')})" for r in rows)
    except Exception:  # noqa: BLE001
        return ""


def _query_from(text: str) -> str:
    words = [w for w in re.sub(r"[{}\[\]\"]", " ", text).split() if len(w) < 20][:15]
    return "crypto news " + " ".join(words) if words else "crypto market news today"


class Msg(BaseModel):
    role: str
    content: str


class ChatReq(BaseModel):
    model: str = "analyst"
    messages: list[Msg]
    max_tokens: int = 600
    temperature: float = 0.2
    response_format: dict | None = None


@app.get("/health")
def health():
    return {"model": f"{MODEL_REPO}/{MODEL_FILE}", "ready": _llm is not None, **_stats,
            "uptime_s": round(time.time() - _stats["started"])}


@app.post("/v1/chat/completions")
async def chat(req: ChatReq, authorization: str | None = Header(default=None)):
    if API_KEY and authorization != f"Bearer {API_KEY}":
        raise HTTPException(401, "bad or missing API key")
    if _llm is None:
        raise HTTPException(503, "model still loading" if _stats["loading"] else f"model failed: {_stats['error']}")
    msgs = [{"role": m.role, "content": m.content} for m in req.messages]
    if req.model.endswith("+search") or req.model.endswith(":online"):
        user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        snippets = await asyncio.to_thread(_search, _query_from(user))
        if snippets and msgs and msgs[0]["role"] == "system":
            msgs[0]["content"] += ("\n\nReal-time web search results (retrieved just now):\n" + snippets +
                                   "\n\nGround your answer in these results. Output valid JSON only.")
    t0 = time.time()

    def run():
        with _lock:
            kw = {"messages": msgs, "max_tokens": min(req.max_tokens, 1200), "temperature": req.temperature}
            if req.response_format and req.response_format.get("type") == "json_object":
                kw["response_format"] = {"type": "json_object"}
            return _llm.create_chat_completion(**kw)

    out = await asyncio.to_thread(run)
    _stats["served"] += 1
    _stats["last_seconds"] = round(time.time() - t0, 1)
    out["model"] = MODEL_FILE
    return out


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860)
