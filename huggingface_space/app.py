"""
Crypto analyst LLM on a free Hugging Face Space: Gradio SDK, CPU Basic hardware (2 vCPU, 16 GB RAM, always on).

The model runs ON the Space's CPU with llama.cpp (default Qwen2.5-3B-Instruct, 4-bit, ~2 GB), so there are no
inference credits, no GPU quota and no daily token limit. It is slow (roughly 1-3 minutes per answer), which suits
the trading engine's use: the LAST fallback for its search roles (market briefing, move attribution) after Groq
and OpenRouter.

API (OpenAI-shaped, what collectors/llm_client.py's "hf" provider calls via HF_BASE_URL):
    POST /v1/chat/completions   {"model": "analyst+search", "messages": [...], "max_tokens": 600}
    GET  /health
A model name ending in "+search" or ":online" first fetches DuckDuckGo results and puts them in the prompt.
The Gradio page at / is a manual tester.

Access: make the Space private; callers send an HF token (Authorization: Bearer hf_...), which the engine does with
its saved HF token. Optional extra lock: Space secret SPACE_API_KEY. Optional variables: MODEL_REPO, MODEL_FILE.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

import gradio as gr
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

MODEL_REPO = os.getenv("MODEL_REPO", "Qwen/Qwen2.5-3B-Instruct-GGUF")
MODEL_FILE = os.getenv("MODEL_FILE", "qwen2.5-3b-instruct-q4_k_m.gguf")
API_KEY = os.getenv("SPACE_API_KEY", "")
CTX = int(os.getenv("CTX", "4096"))

_llm = None
_lock = threading.Lock()                     # llama.cpp is not re-entrant: one generation at a time
_stats = {"started": time.time(), "served": 0, "last_seconds": None, "loading": True, "error": None}


def _load():
    global _llm
    try:
        from huggingface_hub import hf_hub_download
        from llama_cpp import Llama
        _llm = Llama(model_path=hf_hub_download(MODEL_REPO, MODEL_FILE), n_ctx=CTX,
                     n_threads=os.cpu_count() or 2, verbose=False)
    except Exception as e:  # noqa: BLE001
        _stats["error"] = str(e)[:300]
    finally:
        _stats["loading"] = False


threading.Thread(target=_load, daemon=True).start()


def search(query: str, n: int = 5) -> str:
    try:
        from duckduckgo_search import DDGS
        with DDGS() as d:
            rows = list(d.news(query, max_results=n)) or list(d.text(query, max_results=n))
        return "\n".join(f"- {r.get('title', '')}: {r.get('body', '')[:240]} ({r.get('date', '')})" for r in rows)
    except Exception:  # noqa: BLE001
        return ""


def query_from(text: str) -> str:
    words = [w for w in re.sub(r"[{}\[\]\"]", " ", text).split() if len(w) < 20][:15]
    return "crypto news " + " ".join(words) if words else "crypto market news today"


def answer(model_name: str, messages: list[dict], max_tokens: int, temperature: float, json_mode: bool) -> str:
    if _llm is None:
        raise RuntimeError("model still loading" if _stats["loading"] else f"model failed: {_stats['error']}")
    if model_name.endswith("+search") or model_name.endswith(":online"):
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        snippets = search(query_from(user))
        if snippets:
            extra = ("\n\nReal-time web search results (retrieved just now):\n" + snippets +
                     "\n\nGround your answer in these results. Output valid JSON only.")
            if messages and messages[0]["role"] == "system":
                messages[0]["content"] += extra
            else:
                messages.insert(0, {"role": "system", "content": extra.strip()})
    t0 = time.time()
    with _lock:
        kw = {"messages": messages, "max_tokens": min(max_tokens, 1200), "temperature": temperature}
        if json_mode:
            kw["response_format"] = {"type": "json_object"}
        out = _llm.create_chat_completion(**kw)
    _stats["served"] += 1
    _stats["last_seconds"] = round(time.time() - t0, 1)
    return out["choices"][0]["message"]["content"] or ""


api = FastAPI(title="Crypto Analyst LLM")


class Msg(BaseModel):
    role: str
    content: str


class ChatReq(BaseModel):
    model: str = "analyst"
    messages: list[Msg]
    max_tokens: int = 600
    temperature: float = 0.2
    response_format: dict | None = None


@api.get("/health")
def health():
    return {"model": f"{MODEL_REPO}/{MODEL_FILE}", "ready": _llm is not None, **_stats,
            "uptime_s": round(time.time() - _stats["started"])}


@api.post("/v1/chat/completions")
def chat(req: ChatReq, authorization: str | None = Header(default=None)):
    if API_KEY and authorization != f"Bearer {API_KEY}":
        raise HTTPException(401, "bad or missing API key")
    msgs = [{"role": m.role, "content": m.content} for m in req.messages]
    json_mode = bool(req.response_format and req.response_format.get("type") == "json_object")
    try:
        text = answer(req.model, msgs, req.max_tokens, req.temperature, json_mode)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, str(e)[:200]) from e
    return {"id": f"chatcmpl-{int(time.time() * 1000)}", "object": "chat.completion", "created": int(time.time()),
            "model": MODEL_FILE, "choices": [{"index": 0, "finish_reason": "stop",
                                              "message": {"role": "assistant", "content": text}}],
            "usage": {"total_tokens": 0}}


def ui(question: str, use_search: bool) -> str:
    sys_msg = {"role": "system", "content": "You are a crypto market analyst. Reply in JSON: {\"summary\": \"...\"}"}
    try:
        out = answer("analyst+search" if use_search else "analyst",
                     [sys_msg, {"role": "user", "content": question}], 400, 0.2, True)
    except Exception as e:  # noqa: BLE001
        return f"Not ready: {e}"
    try:
        return json.dumps(json.loads(out), indent=2)
    except ValueError:
        return out


demo = gr.Interface(fn=ui, inputs=[gr.Textbox(label="Question", value="Why did Bitcoin move in the last 24 hours?"),
                                   gr.Checkbox(label="Web search first", value=True)],
                    outputs=gr.Code(label="Answer", language="json"), title="Crypto Analyst LLM (CPU)",
                    description="Manual tester (1-3 minutes per answer on the free CPU). "
                                "The trading engine calls /v1/chat/completions.")
app = gr.mount_gradio_app(api, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860)
