"""Smoke tests for the strategy registry — no DB, no Angel, pure imports."""

from __future__ import annotations

import os

# Provide minimal env so `settings` doesn't blow up if anything imports it.
os.environ.setdefault("PG_PASSWORD", "test")
os.environ.setdefault("ANGEL_API_KEY", "test")
os.environ.setdefault("ANGEL_CLIENT_CODE", "test")
os.environ.setdefault("ANGEL_PASSWORD", "test")
os.environ.setdefault("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")  # valid base32
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "test-secret-do-not-use")

from src.engine.v2_engine import StrategyV2  # noqa: E402
from src.strategies.registry import all_strategies, get, names  # noqa: E402


def test_registry_loads_all_strategies():
    reg = all_strategies()
    assert set(reg.keys()) == {
        "S1_user_pyramid",
        "S6_tiered_exit",
        "S10_rsi_filter",
        "S14_concentrated",
        "S23_s20_equity8",
        "S28_s23_nifty",
        "S29_s23_sensex",
        "S31_s24_persist",
        "S50_drop3_e15_alloc10_vol11",
        "S228_multiregime_bear_pyr_small",
        "S283_mm_dma_classic",
        "S404_s392_side_only",
    }


def test_default_portfolio_strategies_are_registered():
    """The 5 strategies referenced in config/portfolios.yaml must all resolve."""
    for name in (
        "S6_tiered_exit",
        "S14_concentrated",
        "S23_s20_equity8",
        "S29_s23_sensex",
        "S31_s24_persist",
    ):
        s = get(name)
        assert s.name == name


def test_each_is_a_strategyv2_instance():
    for n in names():
        s = get(n)
        assert isinstance(s, StrategyV2)
        assert s.name == n


def test_strategy_unknown_raises():
    import pytest
    with pytest.raises(KeyError):
        get("not_a_real_strategy")


def test_s1_parameters_match_upstream():
    s = get("S1_user_pyramid")
    assert s.fall_threshold == -0.05
    assert s.pyramid_levels == ((-0.10, 5000.0), (-0.15, 5000.0))
    assert s.exit_tiers == ((0.25, 0.5), (0.40, 1.0))
    assert s.allocation_per_trade == 5000.0


def test_s6_parameters_match_upstream():
    s = get("S6_tiered_exit")
    assert s.fall_threshold == -0.05
    assert s.exit_tiers == ((0.15, 0.33), (0.30, 0.5), (0.50, 1.0))
    assert s.allocation_per_trade == 10000.0


def test_s10_parameters_match_upstream():
    s = get("S10_rsi_filter")
    assert s.fall_threshold == -0.05
    assert s.rsi_max == 35.0
    assert s.exit_tiers == ((0.30, 1.0),)


def test_s14_parameters_match_upstream():
    s = get("S14_concentrated")
    assert s.fall_threshold == -0.05
    assert s.exit_tiers == ((0.35, 1.0),)
    assert s.max_new_buys_per_day == 1
    assert s.allocation_per_trade == 25000.0


def test_s23_parameters_match_upstream():
    s = get("S23_s20_equity8")
    assert s.fall_threshold == -0.05
    assert s.volume_spike_min == 1.3
    assert s.pyramid_levels == ((-0.10, 0.04),)
    assert s.pyramid_basis == "avg"
    assert s.pyramid_volume_filter is True
    assert s.exit_tiers == ((0.25, 0.5), (0.40, 1.0))
    assert s.allocation_mode == "pct_equity"
    assert s.allocation_pct == 0.08


def test_s28_parameters_match_upstream():
    s = get("S28_s23_nifty")
    # S28 = S23 + NIFTY regime gate; everything else identical.
    assert s.fall_threshold == -0.05
    assert s.volume_spike_min == 1.3
    assert s.pyramid_levels == ((-0.10, 0.04),)
    assert s.allocation_mode == "pct_equity"
    assert s.allocation_pct == 0.08
    assert s.regime_filter is True
    assert s.regime_source == "NIFTY_50"


def test_s29_parameters_match_upstream():
    s = get("S29_s23_sensex")
    # S29 = S23 + SENSEX regime gate; A/B partner of S28.
    assert s.fall_threshold == -0.05
    assert s.volume_spike_min == 1.3
    assert s.pyramid_levels == ((-0.10, 0.04),)
    assert s.allocation_mode == "pct_equity"
    assert s.allocation_pct == 0.08
    assert s.regime_filter is True
    assert s.regime_source == "SENSEX"


def test_s31_parameters_match_upstream():
    s = get("S31_s24_persist")
    assert s.fall_threshold == -0.05
    assert s.volume_spike_min is None
    assert s.pyramid_levels == ((-0.10, 0.04),)
    assert s.pyramid_volume_filter is False
    assert s.exit_tiers == ((0.25, 0.5), (0.40, 1.0))
    assert s.allocation_mode == "pct_equity"
    assert s.allocation_pct == 0.08
    assert s.entry_mode == "trigger"
    assert s.trigger_window == ("09:30", "15:00")
    assert s.trigger_persistence_candles == 3
    assert s.trigger_persistence_threshold == -0.03


def test_s50_parameters_match_upstream():
    # strategies_v2.py out["S50_drop3_e15_alloc10_vol11"], line 669.
    s = get("S50_drop3_e15_alloc10_vol11")
    assert s.fall_threshold == -0.030
    assert s.volume_spike_min == 1.1
    assert s.pyramid_levels == ((-0.10, 0.05),)
    assert s.pyramid_basis == "avg"
    assert s.pyramid_volume_filter is True
    assert s.exit_tiers == ((0.15, 0.5), (0.25, 1.0))
    assert s.allocation_mode == "pct_equity"
    assert s.allocation_pct == 0.10


def test_s228_parameters_match_upstream():
    # strategies_v2.py out["S228_multiregime_bear_pyr_small"], line 4149.
    s = get("S228_multiregime_bear_pyr_small")
    assert s.fall_threshold == -0.030
    assert s.volume_spike_min == 1.1
    assert s.pyramid_levels == ((-0.08, 0.06), (-0.16, 0.05), (-0.25, 0.04))
    assert s.exit_tiers == ((0.15, 0.5), (0.25, 1.0))
    assert s.allocation_mode == "pct_equity"
    assert s.allocation_pct == 0.14
    assert s.scan_times == ("11:00", "14:00")
    assert s.macd_filter == "positive"
    assert s.macd_filter_in_bear_market is True
    assert s.regime_source == "NIFTY_50"
    assert s.vix_bear_threshold == 20.0
    # mode switching
    assert s.mode_params_bull.allocation_pct == 0.16
    assert s.mode_params_bear.allocation_pct == 0.10
    assert s.mode_params_bear.exit_tiers == ((0.10, 0.5), (0.18, 1.0))
    assert s.mode_params_bear.volume_spike_min == 1.3
    assert s.mode_params_bear.sma_above_prev == 20
    assert s.mode_params_bear.pyramid_levels == ((-0.08, 0.04), (-0.16, 0.03), (-0.25, 0.02))
    assert s.mode_params_sideways.allocation_pct == 0.12


def test_s283_parameters_match_upstream():
    # strategies_v2.py out["S283_mm_dma_classic"], line 5361.
    s = get("S283_mm_dma_classic")
    assert s.fall_threshold == -0.030
    assert s.volume_spike_min == 1.1
    assert s.pyramid_levels == ((-0.08, 0.06), (-0.16, 0.05), (-0.25, 0.04))
    assert s.exit_tiers == ((0.15, 0.5), (0.25, 1.0))
    assert s.allocation_mode == "pct_equity"
    assert s.allocation_pct == 0.16
    assert s.scan_times == ("11:00", "14:00")
    assert s.macd_filter == "positive"
    assert s.macd_filter_in_bear_market is True
    assert s.regime_source == "NIFTY_50"
    assert s.vix_bear_threshold == 20.0
    assert s.vix_only_bear is False
    assert s.mode_params_bull.allocation_pct == 0.18
    assert s.mode_params_bull.exit_tiers == ((0.15, 0.5), (0.26, 1.0))
    assert s.mode_params_bear.allocation_pct == 0.14
    assert s.mode_params_bear.exit_tiers == ((0.13, 0.5), (0.22, 1.0))
    assert s.mode_params_bear.volume_spike_min == 1.2
    assert s.mode_params_bear.sma_above_prev == 20
    assert s.mode_params_sideways.allocation_pct == 0.16


def test_s404_parameters_match_upstream():
    # strategies_v2.py out["S404_s392_side_only"], line 6200:
    #   _s283_adaptive_mm(name, bull=_S311, bear=_S311, side=_S392)
    # base chassis == S283_mm_dma_classic; only the adaptive depth ladders differ.
    s = get("S404_s392_side_only")
    S311 = (
        (0.0, ((0.10, 0.5), (0.18, 1.0))),
        (8.0, ((0.14, 0.5), (0.24, 1.0))),
        (15.0, ((0.18, 0.5), (0.32, 1.0))),
    )
    S392 = (
        (0.0, ((0.09, 0.5), (0.16, 1.0))),
        (8.0, ((0.13, 0.5), (0.23, 1.0))),
        (15.0, ((0.18, 0.5), (0.32, 1.0))),
    )
    # S283 chassis carried over verbatim
    assert s.fall_threshold == -0.030
    assert s.volume_spike_min == 1.1
    assert s.pyramid_levels == ((-0.08, 0.06), (-0.16, 0.05), (-0.25, 0.04))
    assert s.exit_tiers == ((0.15, 0.5), (0.25, 1.0))
    assert s.allocation_mode == "pct_equity"
    assert s.allocation_pct == 0.16
    assert s.scan_times == ("11:00", "14:00")
    assert s.macd_filter == "positive"
    assert s.regime_source == "NIFTY_50"
    assert s.vix_bear_threshold == 20.0
    assert s.vix_only_bear is False
    # adaptive depth ladders — the defining feature of S404
    assert s.adaptive_exit_by_depth == S311
    assert s.mode_params_bull.adaptive_exit_by_depth == S311
    assert s.mode_params_bear.adaptive_exit_by_depth == S311
    assert s.mode_params_sideways.adaptive_exit_by_depth == S392
    # mode allocations unchanged from the S283 chassis
    assert s.mode_params_bull.allocation_pct == 0.18
    assert s.mode_params_bear.allocation_pct == 0.14
    assert s.mode_params_sideways.allocation_pct == 0.16
    assert s.mode_params_bear.sma_above_prev == 20
