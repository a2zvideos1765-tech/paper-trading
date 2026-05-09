"""S1: 2-tier pyramid (-10% / -15%), 50% at +25%, 50% at +40%.

The `starting_cash` field gets overridden by the trader to match each portfolio's
configured capital (₹50k or ₹100k). Everything else is parameter-locked here.
"""

from src.engine.v2_engine import StrategyV2


STRATEGY = StrategyV2(
    name="S1_user_pyramid",
    fall_threshold=-0.05,
    pyramid_levels=((-0.10, 5000.0), (-0.15, 5000.0)),
    pyramid_basis="avg",
    exit_tiers=((0.25, 0.5), (0.40, 1.0)),
    allocation_per_trade=5000.0,
)

DESCRIPTION = "Buy ₹5k on -5%; add ₹5k at -10%/-15% from avg; sell 50% at +25%, 50% at +40%."
