from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class ServeStats:
    first_serve_pct: float = 0.60       # 0.0–1.0
    aces: int = 0
    double_faults: int = 0
    service_games_played: int = 0

    # Per-game history (most recent last)
    first_serve_pct_history: list[float] = field(default_factory=list)
    double_faults_history: list[int] = field(default_factory=list)

    @property
    def recent_first_serve_pct(self) -> float:
        """Average over last 2 service games."""
        recent = self.first_serve_pct_history[-2:]
        return sum(recent) / len(recent) if recent else self.first_serve_pct

    @property
    def recent_double_faults(self) -> int:
        """Double faults in last 2 service games."""
        return sum(self.double_faults_history[-2:])


@dataclass
class OddsPoint:
    odds_p1: float
    odds_p2: float
    timestamp: datetime


@dataclass
class MatchState:
    match_id: str
    player1_name: str
    player2_name: str
    surface: str                        # "clay" | "grass" | "hard" | "indoor_hard"
    tournament: str
    current_server: int                 # 1 or 2 (0 = unknown)
    sets_p1: int = 0
    sets_p2: int = 0
    games_in_set_p1: int = 0
    games_in_set_p2: int = 0
    current_set: int = 1
    is_tiebreak: bool = False
    serve_stats_p1: ServeStats = field(default_factory=ServeStats)
    serve_stats_p2: ServeStats = field(default_factory=ServeStats)
    odds_p1: float = 0.0                # best available back odds (decimal)
    odds_p2: float = 0.0
    odds_history: list[OddsPoint] = field(default_factory=list)
    # Ordered list of game winners (1 or 2)
    game_log: list[int] = field(default_factory=list)
    match_duration_mins: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    is_scheduled: bool = False       # True = upcoming, not yet live
    start_time: datetime | None = None

    # ── Derived helpers ────────────────────────────────────────────────────

    def consecutive_games_won_by(self, player: int) -> int:
        """Count how many consecutive games the given player (1 or 2) has won at the end."""
        count = 0
        for winner in reversed(self.game_log):
            if winner == player:
                count += 1
            else:
                break
        return count

    def total_games_played(self) -> int:
        return len(self.game_log)

    def implied_prob_p1(self) -> float:
        if self.odds_p1 <= 1.0:
            return 0.0
        return 1.0 / self.odds_p1

    def implied_prob_p2(self) -> float:
        if self.odds_p2 <= 1.0:
            return 0.0
        return 1.0 / self.odds_p2

    def odds_change_pct_last_n_minutes(self, player: int, minutes: int) -> float:
        """Positive = odds shortened (player favoured more); negative = drifted."""
        now = self.timestamp
        cutoff = now.timestamp() - minutes * 60
        old_points = [p for p in self.odds_history if p.timestamp.timestamp() < cutoff]
        if not old_points:
            return 0.0
        old_odds = old_points[-1].odds_p1 if player == 1 else old_points[-1].odds_p2
        cur_odds = self.odds_p1 if player == 1 else self.odds_p2
        if old_odds <= 0:
            return 0.0
        return (old_odds - cur_odds) / old_odds  # positive = odds shortened

    def games_won_last_n_minutes(self, minutes: int) -> tuple[int, int]:
        """Returns (games_won_p1, games_won_p2) in the last N minutes.
        Approximation: uses game_log recency based on match duration."""
        if self.match_duration_mins <= 0:
            return 0, 0
        mins_per_game = self.match_duration_mins / max(self.total_games_played(), 1)
        games_in_window = max(1, int(minutes / mins_per_game))
        recent = self.game_log[-games_in_window:]
        return recent.count(1), recent.count(2)
