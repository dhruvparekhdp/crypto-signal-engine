"""Thin Ollama client on the standard library only."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

DEFAULT_HOST = "http://127.0.0.1:11434"


class OllamaError(RuntimeError):
    pass


def chat(model: str, system: str, user: str, schema: dict | None = None, think: bool | None = None,
         num_predict: int = 300, temperature: float = 0.1, num_ctx: int = 4096, host: str = DEFAULT_HOST,
         timeout: float = 900.0, top_p: float | None = None, top_k: int | None = None,
         repeat_penalty: float | None = None) -> dict:
    """One non-streaming chat call. Returns text, thinking, and Ollama's own timings."""
    body = {"model": model, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": temperature, "num_predict": num_predict, "num_ctx": num_ctx}}
    for k, v in (("top_p", top_p), ("top_k", top_k), ("repeat_penalty", repeat_penalty)):
        if v is not None:
            body["options"][k] = v
    if schema is not None:
        body["format"] = schema
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(f"{host}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise OllamaError(f"{e.code}: {e.read().decode(errors='replace')[:200]}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise OllamaError(str(e)) from e
    msg = out.get("message", {})
    ev, evd = out.get("eval_count", 0), out.get("eval_duration", 0)
    return {"text": msg.get("content", ""), "thinking": msg.get("thinking", "") or "",
            "wall_s": time.time() - t0, "load_s": out.get("load_duration", 0) / 1e9,
            "prompt_tokens": out.get("prompt_eval_count", 0), "out_tokens": ev,
            "tok_per_s": (ev / (evd / 1e9)) if evd else 0.0}


THINK = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "repeat_penalty": 1.1}   # the recommended thinking-mode sampling
THINK_RETRY = {"temperature": 0.8, "top_p": 0.95, "top_k": 40, "repeat_penalty": 1.25}


def chat_think(model, system, user, **kw) -> dict:
    """A thinking call. Greedy decoding makes thinking models loop forever, and Ollama aborts them with
    'token repeat limit reached'; use the recommended sampling and retry once, more loosely, if it still loops."""
    try:
        return chat(model, system, user, think=True, **THINK, **kw)
    except OllamaError as e:
        if "repeat limit" not in str(e):
            raise
        return chat(model, system, user, think=True, **THINK_RETRY, **kw)
