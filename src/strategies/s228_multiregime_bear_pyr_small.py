"""S228_multiregime_bear_pyr_small.

Copied verbatim from the algo project's strategies_v2.py (out["S228_..."], line 4149).
Upstream note: "S226 but bear pyramid is also adjusted — smaller adds (4%/3%/2%) to
limit capital locked in bear-market losers. Bull pyramid unchanged (6%/5%/4%)."

Multi-regime: each trading day is classified bull/bear/sideways from NIFTY 50's
DMA structure plus an India-VIX fear override (vix_bear_threshold). The matching
ModeParams block overrides the base config for that day.

`starting_cash` is intentionally omitted — the live trader binds it to the
portfolio's capital at replay time.
"""

from src.engine.v2_engine import ModeParams, StrategyV2


STRATEGY = StrategyV2(
    name="S228_multiregime_bear_pyr_small",
    fall_threshold=-0.030,
    volume_spike_min=1.1,
    pyramid_levels=((-0.08, 0.06), (-0.16, 0.05), (-0.25, 0.04)),
    pyramid_basis="avg",
    pyramid_volume_filter=True,
    exit_tiers=((0.15, 0.5), (0.25, 1.0)),
    allocation_mode="pct_equity",
    allocation_pct=0.14,
    scan_times=("11:00", "14:00"),
    macd_filter="positive",
    macd_filter_in_bear_market=True,
    regime_source="NIFTY_50",
    vix_bear_threshold=20.0,
    mode_params_bull=ModeParams(
        fall_threshold=-0.025,
        allocation_pct=0.16,
        exit_tiers=((0.15, 0.5), (0.25, 1.0)),
        volume_spike_min=1.1,
        macd_filter="__off__",
        pyramid_levels=((-0.08, 0.06), (-0.16, 0.05), (-0.25, 0.04)),
    ),
    mode_params_bear=ModeParams(
        fall_threshold=-0.030,
        allocation_pct=0.10,
        exit_tiers=((0.10, 0.5), (0.18, 1.0)),
        volume_spike_min=1.3,
        macd_filter="positive",
        sma_above_prev=20,
        pyramid_levels=((-0.08, 0.04), (-0.16, 0.03), (-0.25, 0.02)),
    ),
    mode_params_sideways=ModeParams(
        fall_threshold=-0.025,
        allocation_pct=0.12,
        exit_tiers=((0.12, 0.5), (0.20, 1.0)),
        volume_spike_min=1.1,
        macd_filter="__off__",
    ),
)

DESCRIPTION = (
    "Multi-regime: smaller bear pyramid adds (4%/3%/2%) to cap capital locked in "
    "bear-market losers; bull pyramid unchanged (6%/5%/4%). 5yr backtest ~757%."
)
