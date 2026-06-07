"""S404_s392_side_only.

Reconstructed verbatim from the algo project's strategies_v2.py, line 6200:

    out["S404_s392_side_only"] = _s283_adaptive_mm(
        "S404_s392_side_only",
        bull_buckets=_S311_BUCKETS, bear_buckets=_S311_BUCKETS,
        side_buckets=_S392_BUCKETS,
    )

How that expands (verified against the upstream factory chain):
  * `_s283_adaptive_mm` builds the S283 chassis via `_s283(name, 0.18, 0.14, 0.16)`
    — byte-for-byte the same config as S283_mm_dma_classic (see s283_mm_dma_classic.py).
  * `_s283_adaptive` then sets the GLOBAL `adaptive_exit_by_depth = _S311_BUCKETS`.
  * `_s283_adaptive_mm` then attaches a per-regime depth-bucket ladder to each mode:
        bull     → _S311_BUCKETS
        bear     → _S311_BUCKETS
        sideways → _S392_BUCKETS   (the "S392 sideways-only" tightening this strategy isolates)

`adaptive_exit_by_depth` picks the exit ladder by the stock's depth below its 90-day
high at entry: the LAST bucket whose min_depth_pct ≤ entry_depth overrides exit_tiers.

Upstream result note: 643.1% / -32.4% over the 5-year backtest.

`starting_cash` is intentionally omitted — the live trader binds it to the
portfolio's capital at replay time.
"""

from src.engine.v2_engine import ModeParams, StrategyV2


# S311 reference depth buckets (strategies_v2.py line 5797 / 5940 — identical).
_S311_BUCKETS = (
    (0.0,  ((0.10, 0.5), (0.18, 1.0))),
    (8.0,  ((0.14, 0.5), (0.24, 1.0))),
    (15.0, ((0.18, 0.5), (0.32, 1.0))),
)
# S392 reference depth buckets (strategies_v2.py line 6158) — tighter sideways ladder.
_S392_BUCKETS = (
    (0.0,  ((0.09, 0.5), (0.16, 1.0))),
    (8.0,  ((0.13, 0.5), (0.23, 1.0))),
    (15.0, ((0.18, 0.5), (0.32, 1.0))),
)


STRATEGY = StrategyV2(
    name="S404_s392_side_only",
    # ---- S283 chassis (== _s283(name, 0.18, 0.14, 0.16)) ----
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
    # ---- global adaptive ladder (_s283_adaptive sets this to _S311_BUCKETS) ----
    adaptive_exit_by_depth=_S311_BUCKETS,
    mode_params_bull=ModeParams(
        fall_threshold=-0.025, allocation_pct=0.18,
        exit_tiers=((0.15, 0.5), (0.26, 1.0)), volume_spike_min=1.1, macd_filter="__off__",
        adaptive_exit_by_depth=_S311_BUCKETS,
    ),
    mode_params_bear=ModeParams(
        fall_threshold=-0.030, allocation_pct=0.14,
        exit_tiers=((0.13, 0.5), (0.22, 1.0)), volume_spike_min=1.2,
        macd_filter="positive", sma_above_prev=20,
        adaptive_exit_by_depth=_S311_BUCKETS,
    ),
    mode_params_sideways=ModeParams(
        fall_threshold=-0.025, allocation_pct=0.16,
        exit_tiers=((0.13, 0.5), (0.23, 1.0)), volume_spike_min=1.1, macd_filter="__off__",
        adaptive_exit_by_depth=_S392_BUCKETS,
    ),
)

DESCRIPTION = (
    "S283 chassis with mode-aware adaptive exits: bull & bear use the S311 depth "
    "ladder, sideways uses the tighter S392 ladder (isolating the sideways "
    "contribution). 5yr backtest ~643%."
)
