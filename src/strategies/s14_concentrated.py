"""S14: max 1 buy/day on the deepest drop, ₹25k allocation, +35% target."""

from src.engine.v2_engine import StrategyV2


STRATEGY = StrategyV2(
    name="S14_concentrated",
    fall_threshold=-0.05,
    exit_tiers=((0.35, 1.0),),
    max_new_buys_per_day=1,
    allocation_per_trade=25000.0,
)

DESCRIPTION = "Max 1 buy/day on deepest drop; ₹25k allocation; +35% exit."
