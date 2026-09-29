"""
Simple script to verify your Hugging Face Space endpoint.
Usage: python test_space.py https://<your-username>-<space-name>.hf.space [HF_TOKEN]
"""

import sys
import httpx

if len(sys.argv) < 2:
    print("Usage: python test_space.py https://<your-username>-<space-name>.hf.space [HF_TOKEN]")
    sys.exit(1)

base_url = sys.argv[1].rstrip("/")
token = sys.argv[2] if len(sys.argv) > 2 else None

headers = {"Content-Type": "application/json"}
if token:
    headers["Authorization"] = f"Bearer {token}"

print(f"1. Checking health: {base_url}/health")
try:
    r = httpx.get(f"{base_url}/health", timeout=10.0)
    print("Health response:", r.status_code, r.text)
except Exception as e:
    print("Health check failed:", e)
    sys.exit(1)

print("\n2. Testing chat completion with web search: /v1/chat/completions")
payload = {
    "model": "analyst:online",
    "messages": [
        {"role": "system", "content": "You are a financial analyst. Reply in JSON: {\"summary\": \"...\"}"},
        {"role": "user", "content": "What happened to Bitcoin in the last 24 hours? Brief me."}
    ],
    "max_tokens": 500,
}

try:
    r = httpx.post(f"{base_url}/v1/chat/completions", json=payload, headers=headers, timeout=60.0)
    print("Chat completion status:", r.status_code)
    print("Response:\n", r.text[:600])
except Exception as e:
    print("Chat completion failed:", e)
    sys.exit(1)
