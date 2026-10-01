"""
Global AI Rate & Quota Governor for Multi-Provider AI Inference.

Enforces zero-alert rate limiting across Groq, Gemini, Hugging Face, and OpenRouter:
- Sliding 60-second window tracker (RPM Governor)
- Rolling 24-hour daily quota tracker (RPD Governor)
- Provider-specific minimum inter-call spacing
- Dynamic elastic failover to available providers
- Thread-safe & async-compatible circular AI event ledger
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("simulator.ai_governor")


@dataclass
class AIEvent:
    event_id: str
    timestamp: str
    provider: str
    model: str
    call_type: str  # "pre_trade", "mirror_check", "dual_post_mortem", "in_flight"
    symbol: str
    direction: str
    latency_ms: int
    status: str  # "SUCCESS", "GOVERNOR_ROUTED", "SYNTHETIC_FALLBACK", "ERROR"
    summary: str
    why_it_worked: str = ""
    why_it_failed: str = ""
    key_takeaway: str = ""
    thinking_trace: str = ""
    tokens: dict[str, int] = field(default_factory=dict)


@dataclass
class ProviderQuota:
    rpm_limit: int
    rpd_limit: int
    min_interval_seconds: float
    request_timestamps: deque[float] = field(default_factory=deque)
    daily_timestamps: deque[float] = field(default_factory=deque)
    last_call_time: float = 0.0
    total_calls_dispatched: int = 0
    total_calls_succeeded: int = 0
    total_failures: int = 0


class GlobalAIGovernor:
    """
    Centralized rate and quota governor enforcing zero 429/402 limit errors
    across all configured AI providers.
    """

    def __init__(self, events_log_path: str = "data/simulator/ai_events.json"):
        self.events_log_path = Path(events_log_path)
        self.events_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.events: deque[AIEvent] = deque(maxlen=300)
        self.event_counter = 0

        # Provider-specific safe rate ceilings
        self.quotas: dict[str, ProviderQuota] = {
            "groq": ProviderQuota(rpm_limit=25, rpd_limit=10000, min_interval_seconds=0.8),
            "gemini": ProviderQuota(rpm_limit=15, rpd_limit=1500, min_interval_seconds=1.2),
            "hf": ProviderQuota(rpm_limit=5, rpd_limit=150, min_interval_seconds=10.0),
            "openrouter": ProviderQuota(rpm_limit=15, rpd_limit=500, min_interval_seconds=3.0),
        }

    def _purge_old_timestamps(self, quota: ProviderQuota, now: float) -> None:
        """Purge sliding 60s and rolling 24h timestamps."""
        while quota.request_timestamps and now - quota.request_timestamps[0] > 60.0:
            quota.request_timestamps.popleft()
        while quota.daily_timestamps and now - quota.daily_timestamps[0] > 86400.0:
            quota.daily_timestamps.popleft()

    async def can_call_provider(self, provider: str) -> bool:
        """Check if a provider is within RPM, RPD, and min-interval limits."""
        async with self.lock:
            quota = self.quotas.get(provider)
            if not quota:
                return False
            now = time.time()
            self._purge_old_timestamps(quota, now)

            if len(quota.request_timestamps) >= quota.rpm_limit:
                return False
            if len(quota.daily_timestamps) >= quota.rpd_limit:
                return False
            if now - quota.last_call_time < quota.min_interval_seconds:
                return False
            return True

    async def reserve_call(self, provider: str) -> bool:
        """Reserve a rate-limit slot for a provider."""
        async with self.lock:
            quota = self.quotas.get(provider)
            if not quota:
                return False
            now = time.time()
            self._purge_old_timestamps(quota, now)

            if len(quota.request_timestamps) >= quota.rpm_limit:
                return False
            if len(quota.daily_timestamps) >= quota.rpd_limit:
                return False
            if now - quota.last_call_time < quota.min_interval_seconds:
                return False

            quota.request_timestamps.append(now)
            quota.daily_timestamps.append(now)
            quota.last_call_time = now
            quota.total_calls_dispatched += 1
            return True

    async def select_best_available_provider(self, priority: list[str] | None = None) -> str | None:
        """Select the highest-priority provider that has safe quota available right now."""
        order = priority or ["gemini", "groq", "openrouter", "hf"]
        for p in order:
            if await self.can_call_provider(p):
                return p
        return None

    def record_event(
        self,
        provider: str,
        model: str,
        call_type: str,
        symbol: str,
        direction: str,
        latency_ms: int,
        status: str,
        summary: str,
        why_it_worked: str = "",
        why_it_failed: str = "",
        key_takeaway: str = "",
        thinking_trace: str = "",
        tokens: dict[str, int] | None = None,
    ) -> AIEvent:
        """Record an AI call event into the circular buffer and persist to disk."""
        self.event_counter += 1
        evt = AIEvent(
            event_id=f"ai-evt-{int(time.time())}-{self.event_counter:04d}",
            timestamp=datetime.now(UTC).isoformat(),
            provider=provider,
            model=model,
            call_type=call_type,
            symbol=symbol,
            direction=direction,
            latency_ms=latency_ms,
            status=status,
            summary=summary,
            why_it_worked=why_it_worked,
            why_it_failed=why_it_failed,
            key_takeaway=key_takeaway,
            thinking_trace=thinking_trace,
            tokens=tokens or {},
        )
        self.events.append(evt)

        # Update provider stats
        quota = self.quotas.get(provider)
        if quota:
            if status == "SUCCESS":
                quota.total_calls_succeeded += 1
            else:
                quota.total_failures += 1

        # Persist recent events to file for dashboard API
        try:
            recent_list = [asdict(e) for e in list(self.events)[-100:]]
            self.events_log_path.write_text(json.dumps(recent_list, indent=2))
        except Exception as e:
            log.warning("failed_to_write_ai_events", error=str(e))

        return evt

    def get_telemetry(self) -> dict[str, Any]:
        """Return real-time quota telemetry and recent events for dashboard display."""
        now = time.time()
        provider_stats = {}
        for name, q in self.quotas.items():
            self._purge_old_timestamps(q, now)
            provider_stats[name] = {
                "rpm_used": len(q.request_timestamps),
                "rpm_limit": q.rpm_limit,
                "rpd_used": len(q.daily_timestamps),
                "rpd_limit": q.rpd_limit,
                "total_dispatched": q.total_calls_dispatched,
                "total_succeeded": q.total_calls_succeeded,
                "total_failures": q.total_failures,
                "status": "ready" if len(q.request_timestamps) < q.rpm_limit and len(q.daily_timestamps) < q.rpd_limit else "cooling_down",
            }

        return {
            "providers": provider_stats,
            "total_events_logged": len(self.events),
            "recent_events": [asdict(e) for e in list(self.events)[-30:]],
        }


# Global singleton instance
governor = GlobalAIGovernor()
