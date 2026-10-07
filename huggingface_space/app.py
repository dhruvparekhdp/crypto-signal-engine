"""
Crypto analyst LLM on a free Hugging Face Space: Gradio SDK, CPU Basic hardware (2 vCPU, 16 GB RAM, always on).

The model runs ON the Space's CPU with llama.cpp (default Qwen2.5-3B-Instruct, 4-bit, ~2 GB), so there are no
inference credits, no GPU quota and no daily token limit. It is slow (roughly 1-3 minutes per answer), which suits
the trading engine's use: the LAST fallback for its search roles (market briefing, move attribution) after Groq
and OpenRouter.

API (OpenAI-shaped, what collectors/llm_client.py's "hf" provider calls via HF_BASE_URL):
    POST /v1/chat/completions   {"model": "analyst+search", "messages": [...], "max_tokens": 600}
    POST /v1/classify           {"texts": [...]}  FinBERT positive/negative/neutral per headline (fast, batched)
    POST /v1/embed              {"texts": [...]}  bge-small sentence vectors (dedupe, similar past events)
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
import sys
import threading
import time

import gradio as gr
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

def _musl_shim() -> None:
    """The prebuilt llama-cpp-python CPU wheels link against musl libc ("libc.musl-x86_64.so.1: cannot open shared
    object file"). packages.txt installs musl-dev; expose its libc under that name, by a symlink in /lib if we may
    write there, else in /tmp with LD_LIBRARY_PATH (read only at process start, hence the re-exec)."""
    name, src = "libc.musl-x86_64.so.1", "/usr/lib/x86_64-linux-musl/libc.so"
    if not os.path.exists(src) or os.path.exists(f"/lib/{name}") or os.environ.get("MUSL_SHIM_DONE"):
        return
    try:
        os.symlink(src, f"/lib/{name}")
        return
    except OSError:
        pass
    shim_dir = "/tmp/musl-shim"
    os.makedirs(shim_dir, exist_ok=True)
    if not os.path.exists(f"{shim_dir}/{name}"):
        os.symlink(src, f"{shim_dir}/{name}")
    os.environ["LD_LIBRARY_PATH"] = shim_dir + ":" + os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["MUSL_SHIM_DONE"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)


_musl_shim()


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

# ZeroGPU hardware refuses to start an app with no @spaces.GPU function ("No @spaces.GPU function detected during
# startup"). Everything here runs on the CPU, so register a no-op GPU function when the `spaces` package exists; on
# CPU Basic hardware the import fails and nothing happens.
try:
    import spaces

    @spaces.GPU(duration=1)
    def _gpu_noop() -> bool:
        return True
except ImportError:
    pass


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


CLASSIFIER_ID = os.getenv("CLASSIFIER_ID", "ProsusAI/finbert")
EMBEDDER_ID = os.getenv("EMBEDDER_ID", "BAAI/bge-small-en-v1.5")
_aux: dict = {}
_aux_lock = threading.Lock()


def _classifier():
    with _aux_lock:
        if "clf" not in _aux:
            from transformers import pipeline
            _aux["clf"] = pipeline("text-classification", model=CLASSIFIER_ID, top_k=None, truncation=True, device=-1)
        return _aux["clf"]


def _embedder():
    with _aux_lock:
        if "emb" not in _aux:
            from sentence_transformers import SentenceTransformer
            _aux["emb"] = SentenceTransformer(EMBEDDER_ID, device="cpu")
        return _aux["emb"]


api = FastAPI(title="Crypto Analyst LLM")             # kept for local tests; the Space uses Gradio's own app


class TextsReq(BaseModel):
    texts: list[str]


def _check(authorization):
    if API_KEY and authorization != f"Bearer {API_KEY}":
        raise HTTPException(401, "bad or missing API key")


@api.post("/v1/classify")
def classify(req: TextsReq, authorization: str | None = Header(default=None)):
    _check(authorization)
    texts = [t[:512] for t in req.texts[:200]]
    t0 = time.time()
    out = _classifier()(texts, batch_size=16)
    rows = [{d["label"].lower(): round(float(d["score"]), 4) for d in r} for r in out]
    return {"model": CLASSIFIER_ID, "results": rows, "seconds": round(time.time() - t0, 2)}


@api.post("/v1/embed")
def embed(req: TextsReq, authorization: str | None = Header(default=None)):
    _check(authorization)
    vecs = _embedder().encode([t[:512] for t in req.texts[:200]], normalize_embeddings=True)
    return {"model": EMBEDDER_ID, "vectors": [[round(float(x), 5) for x in v] for v in vecs]}


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
            "classifier": CLASSIFIER_ID, "classifier_loaded": "clf" in _aux,
            "embedder": EMBEDDER_ID, "embedder_loaded": "emb" in _aux,
            "uptime_s": round(time.time() - _stats["started"])}


@api.post("/v1/chat/completions")
def chat(req: ChatReq, authorization: str | None = Header(default=None)):
    _check(authorization)
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

# On a Gradio Space the platform serves `demo` itself on port 7860, so starting our own server collides with it
# (crashed with "address already in use"). Launch Gradio, then attach the API routes to its FastAPI app.
API_ROUTES = [("/health", health, ["GET"]), ("/v1/chat/completions", chat, ["POST"]),
              ("/v1/classify", classify, ["POST"]), ("/v1/embed", embed, ["POST"])]


def attach_routes(fastapi_app) -> None:
    for path, fn, methods in API_ROUTES:
        fastapi_app.add_api_route(path, fn, methods=methods)
    # Gradio registers a catch-all page route; ours must be matched first
    fastapi_app.router.routes.sort(key=lambda r: 0 if getattr(r, "path", "") in {p for p, _, _ in API_ROUTES} else 1)


if __name__ == "__main__":
    fastapi_app, _, _ = demo.queue(default_concurrency_limit=1).launch(
        server_name="0.0.0.0", server_port=int(os.getenv("PORT", "7860")), prevent_thread_lock=True, show_error=True,
        ssr_mode=False)  # SSR puts a Node proxy on 7860 and Python on 7861; the API routes must be on 7860
    attach_routes(fastapi_app)
    demo.block_thread()
