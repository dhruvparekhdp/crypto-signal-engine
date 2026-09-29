"""
Hugging Face AI Market Analyst Microservice
Provides an OpenAI-compatible /v1/chat/completions endpoint with live DuckDuckGo web search.

Designed to run on Hugging Face Spaces (CPU Free Tier or GPU).
Offloads heavy market-briefing and hourly coin move-attribution from the main trading engine.
"""

import os
import re
import json
import time
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from duckduckgo_search import DDGS
from huggingface_hub import InferenceClient

app = FastAPI(title="Crypto Analyst AI with Web Search", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HF_TOKEN = os.getenv("HF_TOKEN", "")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "Qwen/Qwen2.5-72B-Instruct")
hf_client = InferenceClient(token=HF_TOKEN if HF_TOKEN else None)

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: Optional[str] = "analyst:online"
    messages: List[ChatMessage]
    max_tokens: Optional[int] = 2048
    temperature: Optional[float] = 0.2
    response_format: Optional[Dict[str, Any]] = None

def extract_search_query(user_text: str) -> str:
    """Extract a focused web-search query from the user prompt."""
    query = user_text[:200]
    for noise in ["Current time:", "Brief me.", "Write JSON only", "Reply with JSON"]:
        query = query.replace(noise, "")
    query = re.sub(r"\s+", " ", query).strip()
    return query or "crypto market news bitcoin ethereum"

def search_web(query: str, max_results: int = 5) -> str:
    """Fetch live web snippets using DuckDuckGo (Free, no API key needed)."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            if not results:
                return "No search results returned."
            formatted = []
            for r in results:
                title = r.get("title", "")
                body = r.get("body", "")
                href = r.get("href", "")
                formatted.append(f"• Title: {title}\n  Snippet: {body}\n  Source: {href}")
            return "\n\n".join(formatted)
    except Exception as exc:
        return f"Web search note: live search query '{query}' encountered: {exc}"

@app.get("/")
@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "hf-crypto-analyst",
        "model": DEFAULT_MODEL,
        "search_engine": "duckduckgo",
        "uptime": time.time(),
    }

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, authorization: Optional[str] = Header(None)):
    """OpenAI-compatible chat completion endpoint with automated web-search injection."""
    started = time.time()
    
    system_content = ""
    user_content = ""
    for msg in req.messages:
        if msg.role == "system":
            system_content = msg.content
        elif msg.role == "user":
            user_content = msg.content

    # Determine if search is requested or needed
    needs_search = (
        "online" in (req.model or "")
        or "search" in (req.model or "")
        or "brief" in user_content.lower()
        or "news" in user_content.lower()
        or "moved" in user_content.lower()
    )

    search_context = ""
    if needs_search:
        query = extract_search_query(user_content)
        search_context = search_web(query, max_results=5)

    augmented_system = system_content
    if search_context:
        augmented_system += (
            f"\n\n--- REAL-TIME LIVE WEB SEARCH RESULTS (Retrieved just now) ---\n"
            f"{search_context}\n"
            f"--- END LIVE SEARCH RESULTS ---\n"
            f"Use the verified live search facts above to ground your market analysis and briefing."
        )

    # Prepare messages for HF Inference
    hf_messages = [
        {"role": "system", "content": augmented_system},
        {"role": "user", "content": user_content},
    ]

    chosen_model = DEFAULT_MODEL
    # If caller specified a recognized Hugging Face repo ID, use it
    if req.model and "/" in req.model and not req.model.startswith("analyst"):
        chosen_model = req.model

    try:
        response = hf_client.chat.completions.create(
            model=chosen_model,
            messages=hf_messages,
            max_tokens=req.max_tokens or 1500,
            temperature=req.temperature or 0.2,
        )
        reply_content = response.choices[0].message.content or ""
    except Exception as exc:
        # Fallback to secondary fast model if 72B is queuing
        try:
            fallback_model = "meta-llama/Llama-3.3-70B-Instruct" if chosen_model != "meta-llama/Llama-3.3-70B-Instruct" else "Qwen/Qwen2.5-72B-Instruct"
            response = hf_client.chat.completions.create(
                model=fallback_model,
                messages=hf_messages,
                max_tokens=req.max_tokens or 1500,
                temperature=req.temperature or 0.2,
            )
            reply_content = response.choices[0].message.content or ""
            chosen_model = fallback_model
        except Exception as exc2:
            raise HTTPException(status_code=502, detail=f"HF Inference failed: {exc} | Fallback: {exc2}")

    return {
        "id": f"chatcmpl-hf-{int(time.time()*1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": f"hf/{chosen_model}",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": reply_content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": len(augmented_system) // 4 + len(user_content) // 4,
            "completion_tokens": len(reply_content) // 4,
            "total_tokens": (len(augmented_system) + len(user_content) + len(reply_content)) // 4,
        },
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
