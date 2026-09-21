from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from analysis.football_signals import (
    CleanSheetLikely,
    FootballSignal,
    HeavyFavoriteDominating,
    LateDrawFade,
    LateLead,
    RedCardAdvantage,
)
from analysis.football_state import FootballMatchState
from config.settings import settings

log = structlog.get_logger()

_MAX_SIGNALS_MEMORY = 200


class FootballEngine:
    def __init__(self) -> None:
        self.late_lead = LateLead()
        self.heavy_fav = HeavyFavoriteDominating()
        self.late_draw = LateDrawFade()
        self.red_card = RedCardAdvantage()
        self.clean_sheet = CleanSheetLikely()
        self._cooldowns: dict[tuple[str, str], datetime] = {}
        self._recent_signals: list[FootballSignal] = []

    def process(self, state: FootballMatchState) -> list[FootballSignal]:
        candidates = [
            self.late_lead.analyze(state),
            self.heavy_fav.analyze(state),
            self.late_draw.analyze(state),
            self.red_card.analyze(state),
            self.clean_sheet.analyze(state),
        ]
        fired: list[FootballSignal] = []
        for sig in candidates:
            if sig is None:
                continue
            if sig.confidence < settings.min_confidence:
                log.debug("football_signal_below_threshold",
                          signal_type=sig.signal_type, confidence=sig.confidence)
                continue
            if self._is_on_cooldown(state.match_id, sig.signal_type):
                log.debug("football_signal_on_cooldown",
                          signal_type=sig.signal_type, match_id=state.match_id)
                continue
            self._set_cooldown(state.match_id, sig.signal_type)
            self._recent_signals.append(sig)
            if len(self._recent_signals) > _MAX_SIGNALS_MEMORY:
                self._recent_signals = self._recent_signals[-_MAX_SIGNALS_MEMORY:]
            fired.append(sig)
            log.info("football_signal_fired",
                     signal_type=sig.signal_type,
                     team=sig.team_to_back,
                     match_id=sig.match_id,
                     minute=sig.minute,
                     confidence=sig.confidence)
        return fired

    def get_recent_signals(self, hours: int = 24) -> list[FootballSignal]:
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        return [s for s in self._recent_signals if s.timestamp >= cutoff]

    def _is_on_cooldown(self, match_id: str, signal_type: str) -> bool:
        key = (match_id, signal_type)
        last = self._cooldowns.get(key)
        if last is None:
            return False
        return datetime.now(UTC) - last < timedelta(minutes=settings.signal_cooldown_minutes)

    def _set_cooldown(self, match_id: str, signal_type: str) -> None:
        self._cooldowns[(match_id, signal_type)] = datetime.now(UTC)
