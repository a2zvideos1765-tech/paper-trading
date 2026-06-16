"""Type coercion + semantic validation for per-portfolio strategy overrides.

Both layers must hold the line so a bad override can never silently corrupt
the trader: coerce() rejects type mismatches; validate() rejects semantic
inconsistencies (e.g. trigger_window outside market hours).
"""

from __future__ import annotations

import os

# Provide minimal env so settings doesn't blow up.
os.environ.setdefault("PG_PASSWORD", "test")
os.environ.setdefault("ANGEL_API_KEY", "test")
os.environ.setdefault("ANGEL_CLIENT_CODE", "test")
os.environ.setdefault("ANGEL_PASSWORD", "test")
os.environ.setdefault("ANGEL_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "test-secret")

import pytest

from src.engine.v2_engine import StrategyV2
from src.strategies.registry import get
from src.strategies.schema import FIELD_SCHEMA, PROTECTED_FIELDS
from src.strategies.validation import CoercionError, coerce, coerce_and_apply, validate


# ---------- coerce() ----------

def test_coerce_floats():
    out = coerce({"fall_threshold": "-0.07"})
    assert out["fall_threshold"] == -0.07
    assert isinstance(out["fall_threshold"], float)


def test_coerce_ints_round_trip():
    out = coerce({"entry_lookback_days": "3"})
    assert out["entry_lookback_days"] == 3
    assert isinstance(out["entry_lookback_days"], int)


def test_coerce_int_rejects_fractional():
    with pytest.raises(CoercionError):
        coerce({"entry_lookback_days": "3.5"})


def test_coerce_bool_strings():
    assert coerce({"regime_filter": "true"})["regime_filter"] is True
    assert coerce({"regime_filter": "false"})["regime_filter"] is False
    assert coerce({"regime_filter": True})["regime_filter"] is True


def test_coerce_optional_none():
    assert coerce({"rsi_max": None})["rsi_max"] is None


def test_coerce_optional_value():
    assert coerce({"rsi_max": 35})["rsi_max"] == 35.0


def test_coerce_required_rejects_none():
    with pytest.raises(CoercionError):
        coerce({"fall_threshold": None})


def test_coerce_tuple_str_2():
    out = coerce({"trigger_window": ["09:30", "15:00"]})
    assert out["trigger_window"] == ("09:30", "15:00")


def test_coerce_tuple_pair_list():
    out = coerce({"exit_tiers": [[0.15, 0.33], [0.30, 0.5], [0.50, 1.0]]})
    assert out["exit_tiers"] == ((0.15, 0.33), (0.30, 0.5), (0.50, 1.0))
    assert isinstance(out["exit_tiers"], tuple)


def test_coerce_unknown_field():
    with pytest.raises(CoercionError):
        coerce({"made_up_field": 1})


def test_coerce_protected_field():
    with pytest.raises(CoercionError):
        coerce({"name": "X"})
    with pytest.raises(CoercionError):
        coerce({"starting_cash": 1})


# ---------- validate() ----------

BASE = get("S6_tiered_exit")


def _strat(**kwargs) -> StrategyV2:
    """Build a strategy from S6 + overrides for terse tests."""
    from dataclasses import replace
    return replace(BASE, **kwargs)


def test_validate_clean_strategy_has_no_errors():
    assert validate(BASE) == {}


def test_validate_fall_threshold_must_be_negative():
    errs = validate(_strat(fall_threshold=0.05))
    assert "fall_threshold" in errs


def test_validate_trigger_mode_requires_window_in_market_hours():
    errs = validate(_strat(entry_mode="trigger", trigger_window=("08:00", "10:00")))
    assert "trigger_window" in errs


def test_validate_trigger_window_start_before_end():
    errs = validate(_strat(entry_mode="trigger", trigger_window=("11:00", "10:00")))
    assert "trigger_window" in errs


def test_validate_trigger_mode_accepts_normal_window():
    errs = validate(_strat(entry_mode="trigger", trigger_window=("09:30", "15:00")))
    assert "trigger_window" not in errs


def test_validate_regime_filter_requires_source():
    errs = validate(_strat(regime_filter=True, regime_source=None))
    assert "regime_source" in errs


def test_validate_regime_source_must_be_known():
    errs = validate(_strat(regime_filter=True, regime_source="MIDCAP100"))
    assert "regime_source" in errs


def test_validate_exit_tiers_must_end_at_one():
    errs = validate(_strat(exit_tiers=((0.15, 0.5), (0.30, 0.5))))
    assert "exit_tiers" in errs


def test_validate_exit_tiers_must_be_increasing():
    errs = validate(_strat(exit_tiers=((0.50, 0.5), (0.30, 1.0))))
    # Either the index-keyed error or the top-level — accept either
    assert any(k.startswith("exit_tiers") for k in errs)


def test_validate_pyramid_levels_descending_drop():
    errs = validate(_strat(pyramid_levels=((-0.05, 1000), (-0.03, 1000))))
    assert any(k.startswith("pyramid_levels") for k in errs)


def test_validate_pct_equity_requires_valid_pct():
    errs = validate(_strat(allocation_mode="pct_equity", allocation_pct=0))
    assert "allocation_pct" in errs
    errs = validate(_strat(allocation_mode="pct_equity", allocation_pct=1.5))
    assert "allocation_pct" in errs


def test_validate_fixed_requires_positive_alloc():
    errs = validate(_strat(allocation_mode="fixed", allocation_per_trade=0))
    assert "allocation_per_trade" in errs


def test_validate_unknown_allocation_mode():
    errs = validate(_strat(allocation_mode="other"))
    assert "allocation_mode" in errs


def test_validate_trail_pair_required_together():
    errs = validate(_strat(trail_activate_pct=0.10, trail_drawdown_pct=None))
    assert "trail_drawdown_pct" in errs
    errs = validate(_strat(trail_activate_pct=None, trail_drawdown_pct=0.05))
    assert "trail_activate_pct" in errs


def test_validate_rsi_range():
    assert "rsi_max" in validate(_strat(rsi_max=0))
    assert "rsi_max" in validate(_strat(rsi_max=120))
    assert "rsi_max" not in validate(_strat(rsi_max=35))


def test_validate_volume_spike_min_at_least_one():
    assert "volume_spike_min" in validate(_strat(volume_spike_min=0.5))
    assert "volume_spike_min" not in validate(_strat(volume_spike_min=1.3))


def test_validate_time_stop_positive():
    assert "time_stop_days" in validate(_strat(time_stop_days=0))
    assert "time_stop_days" not in validate(_strat(time_stop_days=10))


def test_coerce_min_entry_cash_optional():
    assert coerce({"min_entry_cash": None})["min_entry_cash"] is None
    assert coerce({"min_entry_cash": 5000})["min_entry_cash"] == 5000.0


def test_validate_min_entry_cash_must_be_positive():
    assert "min_entry_cash" in validate(_strat(min_entry_cash=0))
    assert "min_entry_cash" in validate(_strat(min_entry_cash=-100))
    assert "min_entry_cash" not in validate(_strat(min_entry_cash=5000))
    assert "min_entry_cash" not in validate(_strat(min_entry_cash=None))


# ---------- coerce_and_apply() ----------

def test_coerce_and_apply_empty_returns_base_unchanged():
    s, errs = coerce_and_apply(BASE, {})
    assert s is BASE
    assert errs == {}


def test_coerce_and_apply_happy_path():
    s, errs = coerce_and_apply(BASE, {"fall_threshold": -0.07, "rsi_max": 40})
    assert errs == {}
    assert s.fall_threshold == -0.07
    assert s.rsi_max == 40.0


def test_coerce_and_apply_surfaces_coercion_errors():
    s, errs = coerce_and_apply(BASE, {"fall_threshold": "not a number"})
    assert "fall_threshold" in errs
    assert s is BASE  # unchanged on error


def test_coerce_and_apply_surfaces_validation_errors():
    s, errs = coerce_and_apply(BASE, {"fall_threshold": 0.10})  # type-OK, semantically invalid
    assert "fall_threshold" in errs


# ---------- schema integrity ----------

def test_schema_covers_every_dataclass_field():
    from dataclasses import fields
    declared = {f.name for f in fields(StrategyV2)} - PROTECTED_FIELDS
    described = set(FIELD_SCHEMA.keys())
    assert declared == described, f"missing={declared - described} extra={described - declared}"
