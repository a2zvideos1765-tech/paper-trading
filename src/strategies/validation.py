"""Strategy override coercion + semantic validation.

Two roles:
1. `coerce(raw)` — turn JSONB values (lists, plain numbers, "null") into the
   exact Python types StrategyV2 expects (tuples, None, etc.). Type-only.
2. `validate(strategy)` — run semantic rules against a constructed StrategyV2
   instance. Returns a dict of {field_name: error_message}; empty if all good.

The trader runs `coerce` then `replace(strategy, **coerced)` then `validate`.
A failing `validate` skips that portfolio's tick with a heartbeat detail —
never silently corrupts state.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from src.engine.v2_engine import StrategyV2
from src.strategies.schema import FIELD_SCHEMA, PROTECTED_FIELDS


# ---- Coercion ---------------------------------------------------------

class CoercionError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


def _coerce_one(name: str, kind: str, value: Any) -> Any:
    if value is None:
        if kind.startswith("optional_"):
            return None
        raise CoercionError(name, "value is required (not optional)")

    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise CoercionError(name, f"expected a number, got {value!r}")

    if kind == "int":
        try:
            f = float(value)
            if int(f) != f:
                raise ValueError
            return int(f)
        except (TypeError, ValueError):
            raise CoercionError(name, f"expected an integer, got {value!r}")

    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"true", "1", "yes", "on"}:
                return True
            if v in {"false", "0", "no", "off"}:
                return False
        raise CoercionError(name, f"expected a boolean, got {value!r}")

    if kind == "str":
        if not isinstance(value, str):
            raise CoercionError(name, f"expected a string, got {value!r}")
        return value

    if kind == "optional_float":
        return _coerce_one(name, "float", value)
    if kind == "optional_int":
        return _coerce_one(name, "int", value)
    if kind == "optional_str":
        return _coerce_one(name, "str", value)

    if kind == "tuple_str_2":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise CoercionError(name, "expected a 2-element list of strings")
        return (str(value[0]), str(value[1]))

    if kind == "tuple_pair_list":
        if not isinstance(value, (list, tuple)):
            raise CoercionError(name, "expected a list of [number, number] pairs")
        out: list[tuple[float, float]] = []
        for i, pair in enumerate(value):
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise CoercionError(name, f"row {i}: expected a 2-element [number, number]")
            try:
                out.append((float(pair[0]), float(pair[1])))
            except (TypeError, ValueError):
                raise CoercionError(name, f"row {i}: both values must be numbers")
        return tuple(out)

    raise CoercionError(name, f"unknown kind {kind!r}")


def coerce(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a JSONB blob to a kwargs dict suitable for `replace(strategy, **out)`."""
    out: dict[str, Any] = {}
    for name, value in raw.items():
        if name in PROTECTED_FIELDS:
            raise CoercionError(name, "field is protected and cannot be overridden")
        meta = FIELD_SCHEMA.get(name)
        if not meta:
            raise CoercionError(name, "unknown StrategyV2 field")
        out[name] = _coerce_one(name, meta["kind"], value)
    return out


# ---- Semantic validation ---------------------------------------------

def _check_time_str(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    try:
        h, m = int(value[:2]), int(value[3:])
    except ValueError:
        return False
    return 0 <= h < 24 and 0 <= m < 60


def _time_in_market_hours(value: str) -> bool:
    """09:15 ≤ value ≤ 15:30."""
    if not _check_time_str(value):
        return False
    h, m = int(value[:2]), int(value[3:])
    minutes = h * 60 + m
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


def validate(strategy: StrategyV2) -> dict[str, str]:
    """Return {field: reason} for any rule violations. Empty dict = OK."""
    errs: dict[str, str] = {}

    # fall_threshold must be negative
    if strategy.fall_threshold >= 0:
        errs["fall_threshold"] = "must be negative (a downward move)"

    # entry_mode == 'trigger' → trigger_window valid + within market hours
    if strategy.entry_mode == "trigger":
        tw = strategy.trigger_window
        if not (isinstance(tw, tuple) and len(tw) == 2):
            errs["trigger_window"] = "trigger mode requires [start, end] HH:MM"
        else:
            start, end = tw
            if not (_time_in_market_hours(start) and _time_in_market_hours(end)):
                errs["trigger_window"] = "both times must be HH:MM within 09:15–15:30 IST"
            elif start >= end:
                errs["trigger_window"] = "start must be before end"

    # regime_filter on → regime_source must be set to a known index
    if strategy.regime_filter:
        if strategy.regime_source not in {"NIFTY_50", "SENSEX"}:
            errs["regime_source"] = "regime filter is on; pick NIFTY_50 or SENSEX"

    # pyramid_levels: drop_pct < 0; alloc > 0; descending magnitude
    last_drop_mag = 0.0
    for i, pair in enumerate(strategy.pyramid_levels):
        drop, alloc = pair
        if drop >= 0:
            errs[f"pyramid_levels[{i}]"] = "drop_pct must be negative"
            break
        if alloc <= 0:
            errs[f"pyramid_levels[{i}]"] = "allocation must be > 0"
            break
        mag = abs(drop)
        if mag <= last_drop_mag:
            errs[f"pyramid_levels[{i}]"] = "rows must be ordered by deeper drop (more negative)"
            break
        last_drop_mag = mag

    # exit_tiers: target_pct strictly increasing; final fraction == 1.0
    last_target = -float("inf")
    if not strategy.exit_tiers:
        errs["exit_tiers"] = "at least one tier is required"
    else:
        for i, pair in enumerate(strategy.exit_tiers):
            target, frac = pair
            if target <= last_target:
                errs[f"exit_tiers[{i}]"] = "target_pct must be strictly increasing"
                break
            if not (0 < frac <= 1):
                errs[f"exit_tiers[{i}]"] = "fraction_to_sell must be in (0, 1]"
                break
            last_target = target
        if "exit_tiers" not in errs and not any(k.startswith("exit_tiers[") for k in errs):
            final_frac = strategy.exit_tiers[-1][1]
            if abs(final_frac - 1.0) > 1e-9:
                errs["exit_tiers"] = "final tier's fraction must be 1.0 to fully exit"

    # allocation_mode rules
    if strategy.allocation_mode in ("pct_equity", "pct_cash"):
        if not (0 < strategy.allocation_pct <= 1):
            errs["allocation_pct"] = f"must be in (0, 1] when allocation_mode='{strategy.allocation_mode}'"
    elif strategy.allocation_mode == "fixed":
        if strategy.allocation_per_trade <= 0:
            errs["allocation_per_trade"] = "must be > 0 when allocation_mode='fixed'"
    else:
        errs["allocation_mode"] = "must be 'fixed', 'pct_equity' or 'pct_cash'"

    # trailing stop: both must be set if either is
    a, d = strategy.trail_activate_pct, strategy.trail_drawdown_pct
    if (a is None) != (d is None):
        missing = "trail_drawdown_pct" if a is not None else "trail_activate_pct"
        errs[missing] = "must be set whenever the other trailing-stop field is set"

    # rsi_max in (0, 100)
    if strategy.rsi_max is not None and not (0 < strategy.rsi_max < 100):
        errs["rsi_max"] = "must be in (0, 100)"

    # volume_spike_min ≥ 1.0
    if strategy.volume_spike_min is not None and strategy.volume_spike_min < 1.0:
        errs["volume_spike_min"] = "must be ≥ 1.0 (otherwise the filter never fires)"

    # time_stop_days ≥ 1
    if strategy.time_stop_days is not None and strategy.time_stop_days < 1:
        errs["time_stop_days"] = "must be ≥ 1"

    # macd_filter must be a known mode
    if strategy.macd_filter is not None and strategy.macd_filter not in {"positive", "rising"}:
        errs["macd_filter"] = "must be 'positive' or 'rising'"

    # macd_recent_crossover requires macd_filter='positive'
    if strategy.macd_recent_crossover and strategy.macd_filter != "positive":
        errs["macd_recent_crossover"] = "requires macd_filter='positive'"

    # vix_bear_threshold must be positive when set
    if strategy.vix_bear_threshold is not None and strategy.vix_bear_threshold <= 0:
        errs["vix_bear_threshold"] = "must be > 0"

    # regime_dma_period sane lower bound
    if strategy.regime_dma_period < 20:
        errs["regime_dma_period"] = "must be ≥ 20"

    # sma_above / sma_above_prev only support 10 or 20
    for fld in ("sma_above", "sma_above_prev"):
        v = getattr(strategy, fld)
        if v is not None and v not in (10, 20):
            errs[fld] = "only 10 or 20 are supported"

    # vix_blend clamps must be ordered lo ≤ hi, and both positive
    if strategy.vix_blend_clamp_lo > strategy.vix_blend_clamp_hi:
        errs["vix_blend_clamp_lo"] = "must be ≤ vix_blend_clamp_hi"
    if strategy.vix_blend_clamp_lo <= 0:
        errs["vix_blend_clamp_lo"] = "must be > 0"

    # mean-reversion half-life: fast bound must be ≤ slow bound
    if strategy.mr_halflife_fast_days > strategy.mr_halflife_slow_days:
        errs["mr_halflife_fast_days"] = "must be ≤ mr_halflife_slow_days"

    # exit_tiers_bear, when set, follows the same rules as exit_tiers
    if strategy.exit_tiers_bear:
        last_t = -float("inf")
        for i, pair in enumerate(strategy.exit_tiers_bear):
            target, frac = pair
            if target <= last_t:
                errs[f"exit_tiers_bear[{i}]"] = "target_pct must be strictly increasing"
                break
            if not (0 < frac <= 1):
                errs[f"exit_tiers_bear[{i}]"] = "fraction_to_sell must be in (0, 1]"
                break
            last_t = target
        else:
            if abs(strategy.exit_tiers_bear[-1][1] - 1.0) > 1e-9:
                errs["exit_tiers_bear"] = "final tier's fraction must be 1.0 to fully exit"

    return errs


def coerce_and_apply(base: StrategyV2, raw: dict[str, Any]) -> tuple[StrategyV2, dict[str, str]]:
    """Convenience: coerce + replace + validate. Returns (resulting_strategy, errors).

    If `raw` is empty, returns the base strategy unchanged and an empty error dict.
    Coercion errors are surfaced as field-level entries in the errors dict so the UI
    can render them without distinguishing coercion from validation failures.
    """
    if not raw:
        return base, {}
    coerced: dict[str, Any] = {}
    errs: dict[str, str] = {}
    for name, value in raw.items():
        if name in PROTECTED_FIELDS:
            errs[name] = "field is protected and cannot be overridden"
            continue
        meta = FIELD_SCHEMA.get(name)
        if not meta:
            errs[name] = "unknown StrategyV2 field"
            continue
        try:
            coerced[name] = _coerce_one(name, meta["kind"], value)
        except CoercionError as exc:
            errs[name] = exc.message
    if errs:
        return base, errs
    new_strategy = replace(base, **coerced)
    errs = validate(new_strategy)
    return new_strategy, errs
