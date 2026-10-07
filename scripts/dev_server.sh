#!/bin/sh
# Local dev server for end-to-end checks: own SQLite database, no AI providers, no Telegram, no API keys.
# Live Binance market data is used (free, read-only). Nothing here touches production.
cd "$(dirname "$0")/.." || exit 1
export DATABASE_URL="sqlite+aiosqlite:///./dev_engine.db"
export PORT="${PORT:-8090}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-devpass}"
export GROQ_API_KEY="" OPENROUTER_API_KEY="" GEMINI_API_KEY="" ANTHROPIC_API_KEY="" HF_API_TOKEN="" HF_BASE_URL=""
export TELEGRAM_BOT_TOKEN="" TELEGRAM_CHAT_ID="" TWELVEDATA_API_KEY="" SECRETS_MASTER_KEY="${SECRETS_MASTER_KEY:-}"
export LLM_CALL_LOG="false" EVENT_MONITOR_ENABLED="false" MOVE_ATTRIBUTION_ENABLED="false" MARKET_BRIEFING_ENABLED="false"
exec .venv/bin/python main.py
