"""
Crypto Analyst AI Microservice on Hugging Face (Gradio + FastAPI + ZeroGPU)
Provides:
  1. OpenAI-compatible /v1/chat/completions API with real-time DuckDuckGo web search
  2. Interactive web browser UI for testing queries
"""

import os
import re
import sys
import time
import subprocess
from typing import List, Optional, Dict, Any

# Auto-install duckduckgo-search if missing from Space environment
try:
    from duckduckgo_search import DDGS
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-cache-dir", "duckduckgo-search"])
        from duckduckgo_search import DDGS
    except Exception as e:
        print(f"Warning: Failed to install duckduckgo-search dynamically: {e}")
        DDGS = None

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from huggingface_hub import InferenceClient
import gradio as gr

# Initialize FastAPI app
api_app = FastAPI(title="Crypto Analyst API")

api_app.add_middleware(
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
    """Extract a concise query from the trading engine prompt."""
    query = user_text[:200]
    for noise in ["Current time:", "Brief me.", "Write JSON only", "Reply with JSON", "format:"]:
        query = query.replace(noise, "")
    query = re.sub(r"\s+", " ", query).strip()
    return query or "crypto market news bitcoin ethereum"

def search_web(query: str, max_results: int = 5) -> str:
    """Fetch live web snippets using DuckDuckGo."""
    if DDGS is None:
        return f"Live search unavailable for query: {query}"
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
        return f"Web search note for query '{query}': {exc}"

@api_app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "hf-crypto-analyst",
        "model": DEFAULT_MODEL,
        "search_engine": "duckduckgo" if DDGS is not None else "unavailable",
        "uptime": time.time(),
    }

@api_app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, authorization: Optional[str] = Header(None)):
    """OpenAI-compatible chat completion endpoint for our EC2 trading engine."""
    system_content = ""
    user_content = ""
    for msg in req.messages:
        if msg.role == "system":
            system_content = msg.content
        elif msg.role == "user":
            user_content = msg.content

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

    hf_messages = [
        {"role": "system", "content": augmented_system},
        {"role": "user", "content": user_content},
    ]

    chosen_model = DEFAULT_MODEL
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
        fallback = "meta-llama/Llama-3.3-70B-Instruct" if chosen_model != "meta-llama/Llama-3.3-70B-Instruct" else "Qwen/Qwen2.5-72B-Instruct"
        try:
            response = hf_client.chat.completions.create(
                model=fallback,
                messages=hf_messages,
                max_tokens=req.max_tokens or 1500,
                temperature=req.temperature or 0.2,
            )
            reply_content = response.choices[0].message.content or ""
            chosen_model = fallback
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
                "message": {"role": "assistant", "content": reply_content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": len(augmented_system) // 4 + len(user_content) // 4,
            "completion_tokens": len(reply_content) // 4,
            "total_tokens": (len(augmented_system) + len(user_content) + len(reply_content)) // 4,
        },
    }

# Interactive Gradio UI for browser testing
def web_ui_chat(prompt: str, history):
    query = extract_search_query(prompt)
    search_context = search_web(query, max_results=4)
    messages = [
        {"role": "system", "content": f"You are a crypto research analyst.\n\nLive Search Facts:\n{search_context}"},
        {"role": "user", "content": prompt}
    ]
    try:
        resp = hf_client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=messages,
            max_tokens=1000,
            temperature=0.2
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        return f"Error: {e}"

demo = gr.ChatInterface(
    fn=web_ui_chat,
    title="📈 Crypto Analyst AI (Live Web Search)",
    description="Backend microservice for 24/7 crypto market briefings and price move attributions.",
)

# Mount the FastAPI REST API onto Gradio
app = gr.mount_gradio_app(api_app, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860)
