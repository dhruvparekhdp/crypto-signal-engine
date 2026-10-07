"""Trading frictions. All rates are fractions of notional."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    taker_fee: float = 0.0005        # Binance USD-M VIP0 taker
    maker_fee: float = 0.0002
    gst: float = 0.18                # India: GST on the fee, as the existing cycle sim assumed
    slip_bps: float = 2.0            # adverse, per market fill
    stop_slip_bps: float = 3.0       # extra on stop fills (market order in a fast tape)
    tp_maker: bool = False           # a target fills as a limit order only when asked
    funding: bool = True

    def __post_init__(self):
        # plan Q10: negative fees or slippage would turn costs into income
        for name in ("taker_fee", "maker_fee", "slip_bps", "stop_slip_bps"):
            if getattr(self, name) < 0:
                raise ValueError(f"CostModel.{name} cannot be negative")
        if not 0 <= self.gst <= 1:
            raise ValueError(f"CostModel.gst={self.gst} is outside [0, 1]")

    @property
    def taker(self) -> float:
        return self.taker_fee * (1 + self.gst)

    @property
    def maker(self) -> float:
        return self.maker_fee * (1 + self.gst)

    def round_trip(self) -> float:
        """Entry + exit fees and slippage as a fraction of price, for a stop-out."""
        return 2 * self.taker + 2 * self.slip_bps / 1e4


PRESETS = {
    "india_gst": CostModel(),
    "binance_vip0": CostModel(gst=0.0),
    "optimistic": CostModel(gst=0.0, slip_bps=0.5, stop_slip_bps=0.5, tp_maker=True),
    "stress": CostModel(slip_bps=5.0, stop_slip_bps=8.0),
}
