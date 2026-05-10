"""S28: S23 + real NIFTY 50 regime gate (NIFTY close > 50-DMA).

Replaces the synthetic equal-weight proxy with the actual broad-market index;
the trader primes the engine's regime cache from the DB before each replay.
"""

from src.engine.v2_engine import StrategyV2


STRATEGY = StrategyV2(
    name="S28_s23_nifty",
    fall_threshold=-0.05,
    volume_spike_min=1.3,
    pyramid_levels=((-0.10, 0.04),),
    pyramid_basis="avg",
    pyramid_volume_filter=True,
    exit_tiers=((0.25, 0.5), (0.40, 1.0)),
    allocation_mode="pct_equity",
    allocation_pct=0.08,
    regime_filter=True,
    regime_source="NIFTY_50",
)

DESCRIPTION = "S23 + NIFTY 50 regime gate (close > 50-DMA). Skips entries during broad-market drawdowns."
