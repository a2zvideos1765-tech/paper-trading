"""S31: S24 (intraday trigger entry) + 3-candle persistence filter.

Only fires after the trigger drop holds for 3 consecutive candles below
-3% — filters out one-bar flash dips that immediately bounce.
"""

from src.engine.v2_engine import StrategyV2


STRATEGY = StrategyV2(
    name="S31_s24_persist",
    fall_threshold=-0.05,
    volume_spike_min=None,           # scan-window volume not meaningful in trigger mode
    pyramid_levels=((-0.10, 0.04),),
    pyramid_basis="avg",
    pyramid_volume_filter=False,
    exit_tiers=((0.25, 0.5), (0.40, 1.0)),
    allocation_mode="pct_equity",
    allocation_pct=0.08,
    entry_mode="trigger",
    trigger_window=("09:30", "15:00"),
    trigger_persistence_candles=3,
    trigger_persistence_threshold=-0.03,
)

DESCRIPTION = "Trigger-mode entry, only after the drop persists 3 candles below -3%."
