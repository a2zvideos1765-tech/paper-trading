"""S10: -5% drop AND RSI(14) < 35; single +30% target."""

from src.engine.v2_engine import StrategyV2


STRATEGY = StrategyV2(
    name="S10_rsi_filter",
    fall_threshold=-0.05,
    rsi_max=35.0,
    exit_tiers=((0.30, 1.0),),
    allocation_per_trade=10000.0,
)

DESCRIPTION = "Buy on -5% AND RSI(14)<35; +30% exit."
