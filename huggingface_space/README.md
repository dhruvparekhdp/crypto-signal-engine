---
title: Crypto Analyst LLM
emoji: 📈
colorFrom: blue
colorTo: indigo
sdk: gradio
app_file: app.py
pinned: false
---

# Crypto analyst LLM (Gradio, free CPU Basic)

Runs Qwen2.5-3B-Instruct (4-bit) with llama.cpp on the Space's always-on CPU, with DuckDuckGo search in front,
behind an OpenAI-shaped `/v1/chat/completions` API. No credits, no GPU quota. Slow (about 1-3 minutes per
answer), so the trading engine uses it as the last fallback for market briefings and move explanations.
Keep the Space private: callers send an HF token (`Authorization: Bearer hf_...`).
