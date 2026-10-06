---
title: Crypto Analyst LLM
emoji: 📈
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Crypto analyst LLM on the Space's own CPU

Runs a small open model (default Qwen2.5-3B-Instruct, 4-bit) with llama.cpp on the free CPU Space, with
DuckDuckGo search in front, behind an OpenAI-shaped `/v1/chat/completions` API. No inference credits.
Slow (about 1-3 minutes per answer), so the trading engine uses it as the last fallback for briefings and
move explanations. Set the secret `SPACE_API_KEY`; callers send `Authorization: Bearer <key>`.
