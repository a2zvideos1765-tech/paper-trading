"""S6: single buy on -5%, sell 33% at +15%, 50% at +30%, all at +50%."""

from src.engine.v2_engine import StrategyV2


STRATEGY = StrategyV2(
    name="S6_tiered_exit",
    fall_threshold=-0.05,
    exit_tiers=((0.15, 0.33), (0.30, 0.5), (0.50, 1.0)),
    allocation_per_trade=10000.0,
)

DESCRIPTION = "Single buy on -5%; sell 33% at +15%, 50% at +30%, all at +50%."
