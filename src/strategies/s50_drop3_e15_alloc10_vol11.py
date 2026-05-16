"""S50_drop3_e15_alloc10_vol11.

Copied verbatim from the algo project's strategies_v2.py (out["S50_..."], line 669).
Upstream note: "S47 exits + 10% alloc + 1.1x vol combined. Best-of-round-2 stacked."

`starting_cash` is intentionally omitted — the live trader binds it to the
portfolio's capital at replay time.
"""

from src.engine.v2_engine import StrategyV2


STRATEGY = StrategyV2(
    name="S50_drop3_e15_alloc10_vol11",
    fall_threshold=-0.030,
    volume_spike_min=1.1,
    pyramid_levels=((-0.10, 0.05),),
    pyramid_basis="avg",
    pyramid_volume_filter=True,
    exit_tiers=((0.15, 0.5), (0.25, 1.0)),
    allocation_mode="pct_equity",
    allocation_pct=0.10,
)

DESCRIPTION = "S47 exits + 10% alloc + 1.1x vol combined. Best-of-round-2 stacked."
