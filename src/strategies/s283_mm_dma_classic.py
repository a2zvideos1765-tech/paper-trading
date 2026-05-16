"""S283_mm_dma_classic.

Copied verbatim from the algo project's strategies_v2.py (out["S283_..."], line 5361).
Upstream note: "DMA-based classifier (vix_only_bear=False). Bear = downtrend legs of
both years (quality-gated); bull = recovery legs (wide exits ride them); sideways
= middle."

Multi-regime: each trading day is classified bull/bear/sideways from NIFTY 50's
DMA structure plus an India-VIX fear override. The matching ModeParams block
overrides the base config for that day.

`starting_cash` is intentionally omitted — the live trader binds it to the
portfolio's capital at replay time.
"""

from src.engine.v2_engine import ModeParams, StrategyV2


STRATEGY = StrategyV2(
    name="S283_mm_dma_classic",
    fall_threshold=-0.030,
    volume_spike_min=1.1,
    pyramid_levels=((-0.08, 0.06), (-0.16, 0.05), (-0.25, 0.04)),
    pyramid_basis="avg",
    pyramid_volume_filter=True,
    exit_tiers=((0.15, 0.5), (0.25, 1.0)),
    allocation_mode="pct_equity",
    allocation_pct=0.16,
    scan_times=("11:00", "14:00"),
    macd_filter="positive",
    macd_filter_in_bear_market=True,
    regime_source="NIFTY_50",
    vix_bear_threshold=20.0,
    vix_only_bear=False,
    mode_params_bull=ModeParams(
        fall_threshold=-0.025, allocation_pct=0.18,
        exit_tiers=((0.15, 0.5), (0.26, 1.0)), volume_spike_min=1.1, macd_filter="__off__",
    ),
    mode_params_bear=ModeParams(
        fall_threshold=-0.030, allocation_pct=0.14,
        exit_tiers=((0.13, 0.5), (0.22, 1.0)), volume_spike_min=1.2,
        macd_filter="positive", sma_above_prev=20,
    ),
    mode_params_sideways=ModeParams(
        fall_threshold=-0.025, allocation_pct=0.16,
        exit_tiers=((0.13, 0.5), (0.23, 1.0)), volume_spike_min=1.1, macd_filter="__off__",
    ),
)

DESCRIPTION = (
    "DMA-based regime classifier: bear rides quality-gated downtrend legs, bull "
    "rides recoveries with wide exits, sideways covers the middle. 5yr backtest ~491%."
)
