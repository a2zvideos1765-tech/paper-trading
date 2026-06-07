"""Type schema for the StrategyV2 dataclass — drives the dashboard's parameter editor.

Each StrategyV2 field is categorized by `kind`, which the UI uses to pick an input
widget and which the trader uses to coerce JSONB → tuple/None/etc.

Some fields are intentionally NOT editable from the dashboard (see PROTECTED_FIELDS):
- `name` — identifier; changing it would orphan the registry lookup
- `starting_cash` — managed by the trader (bound to portfolio.capital)
- `scan_times`, `mode_params_*`, `adaptive_exit_by_depth` — variable-length /
  nested structures with no flat editor widget; configured in strategy files only
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from src.engine.v2_engine import StrategyV2


# ---- Per-field schema -------------------------------------------------

# kind values:
#   "float", "int", "bool", "str"
#   "optional_float", "optional_int", "optional_str"
#   "tuple_str_2"        — fixed-length 2-tuple of strings (e.g. trigger_window)
#   "tuple_pair_list"    — variable-length sequence of (number, number) pairs
FIELD_SCHEMA: dict[str, dict[str, Any]] = {
    # Entry
    "fall_threshold": {
        "kind": "float", "group": "Entry",
        "doc": "Drop % vs prior close that triggers entry consideration. Must be negative (e.g. -0.05 = -5%).",
    },
    "entry_lookback_days": {
        "kind": "int", "group": "Entry",
        "doc": "How many days of history the entry signal looks back over.",
    },
    "rsi_max": {
        "kind": "optional_float", "group": "Entry",
        "doc": "Optional RSI(14) ceiling — skip entry if RSI is above this.",
    },
    "volume_spike_min": {
        "kind": "optional_float", "group": "Entry",
        "doc": "Optional volume confirmation: today's volume must be ≥ this × 20-day median.",
    },
    "regime_filter": {
        "kind": "bool", "group": "Entry",
        "doc": "If true, only enter when the index regime (see regime_source) is bullish.",
    },
    "regime_source": {
        "kind": "optional_str", "options": ["NIFTY_50", "SENSEX"], "group": "Entry",
        "doc": "Index used by regime_filter when enabled.",
    },
    "entry_signal": {
        "kind": "str", "group": "Entry",
        "doc": "Which signal generates entries. Default 'drop'.",
    },
    "entry_mode": {
        "kind": "str", "options": ["scan", "trigger"], "group": "Entry",
        "doc": "'scan' = once per day at scan_time; 'trigger' = intraday once condition holds.",
    },
    "scan_time": {
        "kind": "str", "group": "Entry",
        "doc": "HH:MM IST when scan-mode evaluates entries.",
    },
    "trigger_window": {
        "kind": "tuple_str_2", "group": "Entry",
        "doc": "[start, end] HH:MM IST window during which trigger-mode can fire.",
    },
    "trigger_persistence_candles": {
        "kind": "int", "group": "Entry",
        "doc": "Trigger mode: number of consecutive candles the threshold must hold (0 = no filter).",
    },
    "trigger_persistence_threshold": {
        "kind": "float", "group": "Entry",
        "doc": "Drop % the persistence filter checks against (e.g. -0.03 = -3%).",
    },
    "trigger_require_green_candle": {
        "kind": "bool", "group": "Entry",
        "doc": "Trigger mode: require the firing candle to close green.",
    },
    "low_proximity_max": {
        "kind": "optional_float", "group": "Entry",
        "doc": "Optional: max distance from 90-day low (as fraction). E.g. 0.05 = within 5% of low.",
    },
    "entry_below_ma20": {
        "kind": "bool", "group": "Entry",
        "doc": "If true, only enter when close ≤ 20-day MA — rejects dips that are still near all-time highs.",
    },
    "entry_below_high_pct": {
        "kind": "optional_float", "group": "Entry",
        "doc": "Optional: only enter if close is at least this fraction below the 90-day high (e.g. 0.15 = -15%).",
    },
    "sma_above": {
        "kind": "optional_int", "options": [10, 20], "group": "Entry",
        "doc": "Optional: only enter if today's close ≥ this-day SMA (10 or 20). Filters confirmed downtrends.",
    },
    "sma_above_prev": {
        "kind": "optional_int", "options": [10, 20], "group": "Entry",
        "doc": "Optional: only enter if PRIOR day's close ≥ this-day SMA (10 or 20). Bull-pullback friendly.",
    },
    "staged_entry": {
        "kind": "bool", "group": "Entry",
        "doc": "If true, buy half the allocation on signal day, the rest next day if price hasn't bounced.",
    },

    # Regime / momentum gates
    "macd_filter": {
        "kind": "optional_str", "options": ["positive", "rising"], "group": "Regime",
        "doc": "Optional MACD-histogram gate: 'positive' (hist>0) or 'rising' (hist > hist 3 days ago).",
    },
    "macd_filter_in_bear_market": {
        "kind": "bool", "group": "Regime",
        "doc": "If true, the MACD filter is applied only when NIFTY is in a bearish regime.",
    },
    "macd_recent_crossover": {
        "kind": "bool", "group": "Regime",
        "doc": "If true, require MACD hist negative 20 days ago but positive today. Needs macd_filter='positive'.",
    },
    "sma_above_prev_in_bear": {
        "kind": "bool", "group": "Regime",
        "doc": "If true, sma_above_prev is enforced only in bear regime (lifted in bull markets).",
    },
    "regime_dma_period": {
        "kind": "int", "group": "Regime",
        "doc": "DMA period that classifies bull/bear regime for the bear-only gates (default 50; 200 = deep bear only).",
    },
    "nifty_momentum_filter": {
        "kind": "optional_float", "group": "Regime",
        "doc": "Optional: block all new entries when NIFTY's N-day return is below this (e.g. -0.05 = -5%).",
    },
    "nifty_momentum_lookback": {
        "kind": "int", "group": "Regime",
        "doc": "Lookback (trading days) for nifty_momentum_filter.",
    },
    "vix_bear_threshold": {
        "kind": "optional_float", "group": "Regime",
        "doc": "India VIX level above which a day is forced into bear regime (used by multi-regime strategies).",
    },
    "vix_only_bear": {
        "kind": "bool", "group": "Regime",
        "doc": "If true, bear regime is triggered ONLY by VIX (no DMA-based bear classification).",
    },

    # Pyramiding
    "pyramid_levels": {
        "kind": "tuple_pair_list", "group": "Pyramiding",
        "doc": "[(further_drop_pct, allocation_or_fraction), …] additional buys at deeper drops.",
    },
    "pyramid_basis": {
        "kind": "str", "options": ["avg", "entry"], "group": "Pyramiding",
        "doc": "'avg' = drop measured from running avg price; 'entry' = from initial entry price.",
    },
    "pyramid_volume_filter": {
        "kind": "bool", "group": "Pyramiding",
        "doc": "If true, pyramid adds also require the volume confirmation.",
    },

    # Exits
    "exit_tiers": {
        "kind": "tuple_pair_list", "group": "Exits",
        "doc": "[(target_pct, fraction_to_sell), …]. Last fraction must be 1.0 to fully exit.",
    },
    "hard_stop_pct": {
        "kind": "optional_float", "group": "Exits",
        "doc": "Optional hard stop (negative %, e.g. -0.10 = -10%).",
    },
    "time_stop_days": {
        "kind": "optional_int", "group": "Exits",
        "doc": "Optional time stop: exit if not closed within N trading days.",
    },
    "trail_activate_pct": {
        "kind": "optional_float", "group": "Exits",
        "doc": "Trailing stop: activate after gain reaches this %. Pairs with trail_drawdown_pct.",
    },
    "trail_drawdown_pct": {
        "kind": "optional_float", "group": "Exits",
        "doc": "Trailing stop: exit if price drops this % from the post-activation peak.",
    },
    "atr_stop_multiplier": {
        "kind": "optional_float", "group": "Exits",
        "doc": "Optional ATR-based stop: exit if loss exceeds N × entry-day ATR.",
    },
    "exit_tiers_bear": {
        "kind": "tuple_pair_list", "group": "Exits",
        "doc": "Optional bear-regime exit tiers used instead of exit_tiers when NIFTY is below its regime DMA.",
    },
    "patience_sell_after_days": {
        "kind": "optional_int", "group": "Exits",
        "doc": "Optional: after N trading days, sell a still-profitable position that hasn't hit the next tier.",
    },
    "patience_sell_min_profit": {
        "kind": "float", "group": "Exits",
        "doc": "Minimum unrealized gain required for a patience sell to fire (e.g. 0.03 = +3%).",
    },

    # Sizing
    "allocation_mode": {
        "kind": "str", "options": ["fixed", "pct_equity", "pct_cash"], "group": "Sizing",
        "doc": "'fixed' uses allocation_per_trade rupees; 'pct_equity'/'pct_cash' use allocation_pct of equity/cash.",
    },
    "allocation_per_trade": {
        "kind": "float", "group": "Sizing",
        "doc": "Rupees per new entry when allocation_mode='fixed'.",
    },
    "allocation_pct": {
        "kind": "float", "group": "Sizing",
        "doc": "Fraction of equity per new entry when allocation_mode='pct_equity' (0 < x ≤ 1).",
    },
    "max_new_buys_per_day": {
        "kind": "optional_int", "group": "Sizing",
        "doc": "Cap on number of new entries opened on any single day across symbols.",
    },
    "slippage_rate": {
        "kind": "float", "group": "Sizing",
        "doc": "Per-trade slippage applied to fills (0.001 = 10bps).",
    },
    "displace_threshold": {
        "kind": "optional_float", "group": "Sizing",
        "doc": "Optional: when a candidate's drop ≤ this and cash is short, sell a holding to fund it (e.g. -0.10).",
    },
    "displace_sell_rule": {
        "kind": "str", "options": ["smallest_gain", "oldest"], "group": "Sizing",
        "doc": "Which holding to sell when displacement fires: 'smallest_gain' or 'oldest'.",
    },
    "mr_halflife_alloc_boost": {
        "kind": "optional_float", "group": "Sizing",
        "doc": "Optional: boost allocation for fast mean-reverters (e.g. 1.4 = up to 1.4× on the fastest). None = off.",
    },
    "mr_halflife_fast_days": {
        "kind": "float", "group": "Sizing",
        "doc": "Half-life (days) at or below which the full mr_halflife_alloc_boost is applied.",
    },
    "mr_halflife_slow_days": {
        "kind": "float", "group": "Sizing",
        "doc": "Half-life (days) at or above which no boost is applied (factor = 1.0).",
    },

    # Adaptive exits / VIX blend
    "vix_blend_enabled": {
        "kind": "bool", "group": "Adaptive",
        "doc": "If true, scale adaptive exit thresholds by an India-VIX factor. Needs adaptive_exit_by_depth set.",
    },
    "vix_blend_baseline": {
        "kind": "float", "group": "Adaptive",
        "doc": "Neutral VIX level where the blend factor = 1.0 (e.g. 15.0).",
    },
    "vix_blend_slope": {
        "kind": "float", "group": "Adaptive",
        "doc": "Factor change per VIX point above baseline (e.g. 0.02 = +2% width per point).",
    },
    "vix_blend_clamp_lo": {
        "kind": "float", "group": "Adaptive",
        "doc": "Lower clamp on the VIX blend factor (e.g. 0.75).",
    },
    "vix_blend_clamp_hi": {
        "kind": "float", "group": "Adaptive",
        "doc": "Upper clamp on the VIX blend factor (e.g. 1.50).",
    },
}

# Fields the UI must NOT show / overrides table must NOT touch.
#   name, starting_cash  — identity / trader-managed
#   scan_times           — variable-length string tuple; no editor widget
#   mode_params_*        — nested ModeParams dataclasses; configured in strategy files only
PROTECTED_FIELDS = {
    "name", "starting_cash", "scan_times",
    "mode_params_bull", "mode_params_bear", "mode_params_sideways",
    # Variable-length nested ladder ((min_depth, ((sell_pct, frac), ...)), ...) —
    # no flat editor widget; configured in strategy files only (S404 etc.).
    "adaptive_exit_by_depth",
}


def field_defaults() -> dict[str, Any]:
    """Pull current default values straight from the StrategyV2 dataclass."""
    out: dict[str, Any] = {}
    for f in fields(StrategyV2):
        if f.name in PROTECTED_FIELDS:
            continue
        # `default` is the dataclass default, may be a tuple
        out[f.name] = f.default
    return out


def public_schema() -> dict[str, dict[str, Any]]:
    """Schema actually exposed to the UI (defaults filled in, protected fields removed)."""
    defaults = field_defaults()
    out: dict[str, dict[str, Any]] = {}
    for fname, meta in FIELD_SCHEMA.items():
        if fname in PROTECTED_FIELDS:
            continue
        out[fname] = {**meta, "default": _jsonable(defaults.get(fname))}
    return out


def _jsonable(v: Any) -> Any:
    """Tuples → lists for JSON serialisation."""
    if isinstance(v, tuple):
        return [_jsonable(x) for x in v]
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


def assert_schema_covers_dataclass() -> None:
    """Catch drift: every editable StrategyV2 field must appear in FIELD_SCHEMA."""
    declared = {f.name for f in fields(StrategyV2)} - PROTECTED_FIELDS
    described = set(FIELD_SCHEMA.keys())
    missing = declared - described
    extra = described - declared
    if missing or extra:
        raise RuntimeError(
            f"Schema drift between StrategyV2 and FIELD_SCHEMA — missing: {sorted(missing)}; extra: {sorted(extra)}"
        )


# Run the drift check at import time so it blows up loud, fast.
assert_schema_covers_dataclass()
