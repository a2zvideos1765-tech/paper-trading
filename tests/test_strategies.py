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


def test_registry_loads_three_strategies():
    reg = all_strategies()
    assert set(reg.keys()) == {"S1_user_pyramid", "S6_tiered_exit", "S10_rsi_filter"}


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
