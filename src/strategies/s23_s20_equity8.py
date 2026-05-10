"""S23: S20 with % of total equity sizing — 8% of equity initial, 4% pyramid.

Volume-spike entry filter (≥1.3× 20-day pre-scan avg), single -10% pyramid add
with volume re-confirmation, tiered exit (50% at +25%, 50% at +40%).
"""

from src.engine.v2_engine import StrategyV2


STRATEGY = StrategyV2(
    name="S23_s20_equity8",
    fall_threshold=-0.05,
    volume_spike_min=1.3,
    pyramid_levels=((-0.10, 0.04),),  # 4% of equity on -10% drop
    pyramid_basis="avg",
    pyramid_volume_filter=True,
    exit_tiers=((0.25, 0.5), (0.40, 1.0)),
    allocation_mode="pct_equity",
    allocation_pct=0.08,  # 8% of total equity for the initial buy
)

DESCRIPTION = (
    "S20 with %-of-equity sizing: 8% initial, 4% pyramid. Scales with portfolio "
    "growth including unrealized gains."
)
