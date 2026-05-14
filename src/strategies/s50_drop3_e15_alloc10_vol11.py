"""S50: light-touch dip buy — -3% drop with 1.1x volume confirmation, +15% exit, 10% of equity.

A small-allocation, fast-cycle variant tuned for shallower dips than the S6/S23
family. The volume-spike floor (1.1x 20-day pre-scan average) filters out
sleepy down-days where -3% is just noise.
"""

from src.engine.v2_engine import StrategyV2


STRATEGY = StrategyV2(
    name="S50_drop3_e15_alloc10_vol11",
    fall_threshold=-0.03,               # buy on a -3% drop
    volume_spike_min=1.1,               # need a 1.1x volume spike vs 20-day avg
    exit_tiers=((0.15, 1.0),),          # single full exit at +15%
    allocation_mode="pct_equity",
    allocation_pct=0.10,                # 10% of equity per trade
)

DESCRIPTION = (
    "Buy on -3% drop with 1.1x volume confirmation; full exit at +15%; "
    "10% of equity per trade."
)
