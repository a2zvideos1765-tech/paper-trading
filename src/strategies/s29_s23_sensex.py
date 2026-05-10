"""S29: S23 + real SENSEX regime gate (SENSEX close > 50-DMA).

A/B partner of S28 — same body as S23, swap NIFTY for SENSEX as the regime
index. The trader primes the engine's regime cache from the DB before each
replay.
"""

from src.engine.v2_engine import StrategyV2


STRATEGY = StrategyV2(
    name="S29_s23_sensex",
    fall_threshold=-0.05,
    volume_spike_min=1.3,
    pyramid_levels=((-0.10, 0.04),),
    pyramid_basis="avg",
    pyramid_volume_filter=True,
    exit_tiers=((0.25, 0.5), (0.40, 1.0)),
    allocation_mode="pct_equity",
    allocation_pct=0.08,
    regime_filter=True,
    regime_source="SENSEX",
)

DESCRIPTION = "S23 + SENSEX regime gate (close > 50-DMA). NIFTY twin: S28."
