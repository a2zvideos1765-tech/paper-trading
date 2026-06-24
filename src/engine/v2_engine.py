"""V2 backtest engine — vendored from the algo project.

Source: ../i-want-to-build-an-algo/engine_v2.py
Sync rule: keep this file byte-for-byte identical to upstream EXCEPT for the
sections that read market data from CSV files on disk. The live trader has no
such files — it primes in-memory caches from the DB before each replay.

If you update upstream engine_v2.py, re-vendor by copying it here and re-applying
every block marked `# === paper-trading patch ===`. There are five:
  1. regime-index cache + prime_* helpers (NIFTY_50 / SENSEX / INDIA_VIX)
  2. the ATR(14) pandas>=2.2 transform fix in daily_features
  3. _load_regime_index / _load_nifty_extended_close — read from the cache
  4. classify_regime_by_date — read NIFTY + India VIX from the cache
  5. _load_vix_by_date — read India VIX from the cache (vix_blend strategies)
Then run `pytest tests/test_strategies.py` before deploying.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import pandas as pd


# === paper-trading patch ============================================
# Upstream read regime/VIX series from CSV files under data/. The live
# trader populates these in-memory caches from the DB before each replay
# (see src/engine/replay.py). Tests prime them directly.
_REGIME_INDEX_CACHE: dict[str, pd.Series] = {}   # name -> daily-close series, indexed by date
_REGIME_CACHE: dict[str, pd.Series] = {}          # classify_regime_by_date memo, keyed by params
_VIX_SERIES_CACHE: dict | None = None             # _load_vix_by_date memo (date -> float)


def prime_regime_index(name: str, daily_close: pd.Series) -> None:
    """Inject a daily-close series for a regime index (NIFTY_50 / SENSEX /
    INDIA_VIX). `daily_close` is indexed by Python date. Called once per replay."""
    _REGIME_INDEX_CACHE[name] = daily_close


def clear_regime_cache() -> None:
    """Drop all regime caches. The trader calls this at the start of every replay
    so a stale index series can never leak across portfolios/ticks."""
    global _VIX_SERIES_CACHE
    _REGIME_INDEX_CACHE.clear()
    _REGIME_CACHE.clear()
    _VIX_SERIES_CACHE = None
# === end patch ======================================================


@dataclass(frozen=True)
class ModeParams:
    """Parameter overrides applied when a specific market regime (bull/bear/sideways) is active.

    Any field left as None will fall back to the base StrategyV2 value — so you only need to
    specify what should change, not replicate the full config.

    Supported overrideable fields:
      fall_threshold, allocation_pct, exit_tiers, volume_spike_min,
      macd_filter, sma_above_prev, pyramid_levels, max_new_buys_per_day,
      adaptive_exit_by_depth.
    """
    fall_threshold:      float | None = None
    allocation_pct:      float | None = None
    exit_tiers:          tuple[tuple[float, float], ...] | None = None
    volume_spike_min:    float | None = None
    macd_filter:         str   | None = None   # "positive" | "rising" | "__off__" (explicit off)
    sma_above_prev:      int   | None = None   # 10, 20, 50 | "__off__" (use -1)
    pyramid_levels:      tuple[tuple[float, float], ...] | None = None
    max_new_buys_per_day: int  | None = None
    # Per-regime adaptive exit ladder — overrides strategy.adaptive_exit_by_depth when set.
    # Same format: ((min_depth_pct, ((sell_pct, frac), ...)), ...).
    adaptive_exit_by_depth: tuple[tuple[float, tuple[tuple[float, float], ...]], ...] | None = None


def _meff(mode: "ModeParams | None", field: str, base):
    """Return mode override if set, else base StrategyV2 value."""
    if mode is not None:
        v = getattr(mode, field, None)
        if v is not None:
            return v
    return base


@dataclass(frozen=True)
class StrategyV2:
    name: str = "default"

    # Entry
    fall_threshold: float = -0.05
    entry_lookback_days: int = 1  # 1 = single-day vs prev close; N = N-day cumulative vs N-day-ago close
    rsi_max: float | None = None
    volume_spike_min: float | None = None  # ratio vs 20-day avg same-window volume
    regime_filter: bool = False  # require equal-weight universe proxy > 50-DMA

    # Pyramiding (averaging down)
    pyramid_levels: tuple[tuple[float, float], ...] = ()  # ((drop_pct_from_basis, allocation_rupees), ...)
    pyramid_basis: str = "avg"  # "avg" (running avg_price) or "entry" (initial entry price)

    # Exit tiers — ordered (profit_pct_from_avg, fraction_of_current_qty)
    exit_tiers: tuple[tuple[float, float], ...] = ((0.30, 1.0),)

    # Bear-regime exit tiers: when NIFTY is below its regime DMA (bear market), use these
    # tiers instead of exit_tiers.  Default None → always use exit_tiers (backward-compatible).
    # Rationale: in bear markets, positions recover partially but often don't reach the normal
    # +15% / +25% targets — lowering to +10% / +18% realises those sluggish recoveries.
    # Uses the same regime_source and regime_dma_period as MACD/SMA bear-market gates.
    # Has no effect unless regime_source is set (or the strategy uses macd_filter_in_bear_market).
    exit_tiers_bear: tuple[tuple[float, float], ...] | None = None

    # Adaptive exits: per-position exit tiers chosen by the stock's depth-below-90d-high
    # at first entry. Ordered (min_depth_pct, tiers) buckets ascending — the LAST bucket
    # whose min_depth_pct ≤ entry_depth_pct is selected. Snapshot at entry, then locked
    # (pyramid adds do NOT re-snapshot). Overrides exit_tiers / exit_tiers_bear / mode tiers.
    # Rationale: in our universe a -3% drop on a shallow pullback gives a ~12% bounce, while
    # a -3% drop on a stock already 20% below its 90d high gives a 25%+ V-recovery. One
    # universal exit ladder can't capture both — adaptive tiers route width by depth.
    adaptive_exit_by_depth: tuple[tuple[float, tuple[tuple[float, float], ...]], ...] | None = None

    # Stops
    hard_stop_pct: float | None = None  # e.g. -0.30 from avg_price → close entire position
    time_stop_days: int | None = None
    trail_activate_pct: float | None = None  # e.g. 0.20 — activate trailing stop after this gain
    trail_drawdown_pct: float | None = None  # e.g. 0.10 — sell when price < peak * (1 - this)

    # Sizing
    allocation_mode: str = "fixed"  # "fixed" or "pct_equity"
    allocation_per_trade: float = 10000.0
    allocation_pct: float = 0.05

    # Iteration 2 additions
    atr_stop_multiplier: float | None = None  # snapshot ATR(14) at entry; stop = avg_price - mult * entry_atr
    entry_signal: str = "drop"  # "drop" or "drop_and_bollinger" (also requires close <= bb_lower20)
    pyramid_volume_filter: bool = False  # require volume_spike on each pyramid add
    entry_below_ma20: bool = False  # only enter if close <= 20-day MA (bb_mid20). Rejects ATH dips.

    # Iteration 4 additions
    entry_mode: str = "scan"  # "scan" (snapshot at scan_time) or "trigger" (first intraday cross of fall_threshold)
    trigger_window: tuple[str, str] = ("09:30", "15:00")  # earliest/latest candle times for trigger firing
    low_proximity_max: float | None = None  # buy only when close is within this fraction of 90-day low (e.g. 0.08)

    # Iteration 5 additions
    regime_source: str | None = None  # None = synthetic equal-weight proxy; "NIFTY_50" or "SENSEX" = real index close > 50-DMA
    trigger_persistence_candles: int = 0  # require next N intraday candles to also be below trigger_persistence_threshold
    trigger_persistence_threshold: float = -0.03  # the must-hold-below level for persistence_candles
    trigger_require_green_candle: bool = False  # trigger candle must close >= open (buyer absorption)

    # Iteration 7 — Round 4 additions
    # MACD histogram filter on entry candidates.
    #   "positive"  → macd_hist > 0   (momentum turned bullish; in uptrend territory)
    #   "rising"    → macd_hist today > macd_hist 3 days ago  (momentum improving, even if negative)
    # "rising" is the divergence proxy — selling pressure shrinking even while price is down.
    macd_filter: str | None = None

    # Staged initial entry: buy half the allocation on signal day, the other half the NEXT
    # trading day if and only if the stock hasn't bounced above its initial entry price.
    # Lowers average entry price in slow-recovery setups without a large pyramid drawdown.
    staged_entry: bool = False

    # When True, the MACD filter (macd_filter field) is applied ONLY when NIFTY is in a
    # bearish regime (close < 50-DMA). In bull regimes the MACD gate is lifted so good
    # intraday dips in uptrending markets are not rejected.
    # Has no effect if macd_filter is None. Uses regime_source ("NIFTY_50" by default).
    macd_filter_in_bear_market: bool = False

    # NIFTY momentum filter: skip all new entries on days when NIFTY's N-day rolling
    # return is below this threshold.
    # Example: nifty_momentum_filter=-0.05, nifty_momentum_lookback=20 → block entries
    # when NIFTY fell >5% over the past 20 trading days (rapid-correction detector).
    # Unlike the 50-DMA regime gate (which is slow to flip), this responds within weeks
    # to sharp sell-offs like the Oct-2024 to Mar-2025 correction.
    # Default None → disabled, backward-compatible.
    nifty_momentum_filter: float | None = None
    nifty_momentum_lookback: int = 20

    # Iteration 7 additions
    # Patience sell: after N trading days, if unrealized return >= patience_sell_min_profit
    # but still below the next exit tier target, sell the remaining position at market close.
    # Only fires on PROFITABLE positions (no stop-loss behaviour; no re-entry loop risk).
    # Default None → disabled, backward-compatible with all prior strategies.
    patience_sell_after_days: int | None = None   # e.g. 60
    patience_sell_min_profit: float = 0.0         # e.g. 0.03 → only accept if ≥ +3% unrealized

    # Round 14 addition
    # "Recent MACD crossover" filter: only accept entries where the MACD histogram was
    # NEGATIVE 20 trading days ago but is POSITIVE today.
    # Requires macd_filter="positive" to also be set (raises if macd_filter is None).
    # Why: stocks in a long bull run keep MACD positive for months — a single-day drop
    # while MACD is "residually positive" is low-quality. Requiring a recent zero-crossing
    # ensures the stock was genuinely oversold (negative MACD) and is now genuinely
    # recovering (positive MACD), just like 2022-23 genuine reversals.
    macd_recent_crossover: bool = False  # Default False → backward-compatible

    # Round 15 addition
    # "Distance from 90-day high" filter: only enter if the stock's close is at least
    # entry_below_high_pct below its 90-day rolling high.
    # e.g. entry_below_high_pct=0.15 → close ≤ high_90d * (1 - 0.15) → stock down ≥15% from 90d high.
    # Hypothesis: stocks near ATH that drop 3% are "dips in uptrend" (low mean-reversion edge).
    # Stocks 15%+ below their 90-day high are in genuine deep correction where reversals are stronger.
    # Default None → disabled, backward-compatible.
    entry_below_high_pct: float | None = None

    # SMA above filter: only enter if today's scan close >= N-day SMA of prior closes.
    # e.g. sma_above=20 → close >= 20-DMA (stock in uptrend, -3% is a pullback not a breakdown).
    # This filters out stocks in confirmed downtrends where mean reversion fails.
    # Supported values: 10 (uses sma10 column) or 20 (reuses bb_mid20). Default None → disabled.
    sma_above: int | None = None

    # SMA above filter using PREVIOUS day's close (not today's scan close).
    # e.g. sma_above_prev=10 → prev_close >= sma10 (stock was above SMA yesterday before today's drop).
    # Better for bull-year pullbacks: stock was 103 (above SMA), drops to 100 today → allowed.
    # In bear market: stock was 97 (below SMA), drops further → blocked.
    # Contrast with sma_above: that compares today's (already-dropped) close, which falls below SMA
    # even on legitimate bull-market pullbacks, destroying 2021-22 entry count.
    # Supported values: 10 (uses sma10) or 20 (reuses bb_mid20). Default None → disabled.
    sma_above_prev: int | None = None

    # When True, sma_above_prev is applied ONLY in bear regime (same logic as macd_filter_in_bear_market).
    # In bull market (NIFTY > 50-DMA): sma_above_prev is lifted → allows deep-correction entries.
    # In bear market: sma_above_prev enforced → only recovery-phase stocks (above SMA) can be bought.
    # Uses regime_source for the bear/bull determination.
    # Has no effect if sma_above_prev is None. Default False → always applied when sma_above_prev is set.
    sma_above_prev_in_bear: bool = False

    # DMA period used to classify bear/bull regime for macd_filter_in_bear_market and
    # sma_above_prev_in_bear.  Default 50 → identical to all prior strategies (backward-compatible).
    # Set to 200 to only activate the bear-market quality gates when the index is in a DEEP bear
    # (below 200-DMA) rather than a shallow correction (below 50-DMA but above 200-DMA).
    # This lets the quality gate fire in 2022's global bear (below 200-DMA) while staying OFF
    # during the shallower 2024-25 India correction where stocks were still above their long-run trend.
    regime_dma_period: int = 50

    # Multiple scan windows per day.
    # When set, entries are attempted at EACH time in the tuple (e.g. ("11:00", "14:00")).
    # Volume ratios for each window are computed independently so the 1.1× gate is calibrated
    # to that time-of-day's typical volume accumulation.
    # Pyramid adds and staged-entry second tranches always use scan_time (the primary window).
    scan_times: tuple[str, ...] | None = None  # None → use (scan_time,)

    # Position displacement for extreme intraday drops.
    # When a candidate's change <= displace_threshold (e.g. -0.10) and cash is insufficient,
    # sell one existing holding to fund the trade.  The holding sold is chosen by displace_sell_rule:
    #   "smallest_gain" → sell the holding with the smallest unrealized gain (or biggest loss)
    #   "oldest"        → sell the holding that has been held the longest
    # Displacement never fires if holdings is empty or if cash is already sufficient.
    displace_threshold: float | None = None   # e.g. -0.10 → sell a holding to buy a -10%+ stock
    displace_sell_rule: str = "smallest_gain"

    # Multi-regime mode switching.
    # When any of these is set, the engine classifies each trading day as "bull", "bear",
    # or "sideways" using NIFTY 50 (extended CSV, 2018→present):
    #   bull     → NIFTY close > 50-DMA  AND  20-DMA > 50-DMA  (established uptrend)
    #   bear     → NIFTY close < 50-DMA  AND  20-DMA < 50-DMA  (established downtrend)
    #   sideways → all other days (transitional / chopping)
    # Any field in ModeParams that is not None overrides the corresponding StrategyV2 field
    # for that day.  Fields left as None use the base StrategyV2 value.
    # All three can be None simultaneously → no regime switching (backward-compatible).
    # Optional: vix_bear_threshold (default 20.0) — if India VIX > threshold AND NIFTY < 50-DMA,
    # that day is forced into bear mode (high-fear corrections treated as bear regardless of 20-DMA).
    mode_params_bull:     ModeParams | None = None
    mode_params_bear:     ModeParams | None = None
    mode_params_sideways: ModeParams | None = None
    vix_bear_threshold:   float | None = 20.0   # None → VIX not used
    vix_only_bear:        bool = False          # True → bear only via VIX (no DMA-based bear)

    # Round 46 — VIX-blended adaptive exits.
    # When enabled, each selected adaptive tier's profit threshold is multiplied by a factor
    # derived from today's India VIX: factor = clamp(1 + (vix - baseline) * slope, lo, hi).
    # High VIX → factor > 1 → wider exits (capitulation regimes reward holding longer).
    # Low VIX → factor < 1 → tighter exits (compressed-vol regimes, smaller bounces).
    # Has no effect when adaptive_exit_by_depth is None (ignored for non-adaptive strategies).
    vix_blend_enabled:  bool  = False
    vix_blend_baseline: float = 15.0       # neutral VIX level (factor = 1.0 here)
    vix_blend_slope:    float = 1.0 / 50.0 # scaling per VIX point above baseline (0.02 = 2pp/pt)
    vix_blend_clamp_lo: float = 0.75       # minimum factor
    vix_blend_clamp_hi: float = 1.50       # maximum factor

    # Round 47 — Per-symbol mean-reversion half-life allocation boost.
    # When mr_halflife_alloc_boost is set, allocation is multiplied by a factor that
    # linearly interpolates between boost (at halflife ≤ fast_days) and 1.0 (at ≥ slow_days).
    # Symbols with short half-lives (fast reverters like liquid large-caps) get proportionally
    # more capital. Has no effect when None (backward-compatible).
    mr_halflife_alloc_boost: float | None = None   # e.g. 1.4 → up to 1.4× alloc on fastest reverters
    mr_halflife_fast_days: float = 8.0             # ≤ this → full boost applied
    mr_halflife_slow_days: float = 30.0            # ≥ this → 1.0× (no boost)

    # Misc
    scan_time: str = "11:00"
    starting_cash: float = 100000.0
    max_new_buys_per_day: int | None = None
    slippage_rate: float = 0.001

    # SIP / variable-deposit support.
    # When min_entry_cash is set, no new BUY orders fire on days when free cash < this amount.
    # Prevents tiny positions where the DP sell charge (₹15.34) forms an outsized % of turnover.
    # Recommended minimum: ₹5,000 (DP charge = 0.31% at that size, acceptable vs 0.89% at ₹2k).
    # Has no effect on pyramid adds or exits — only initial entries are gated.
    # Default None → disabled, backward-compatible with all existing strategies.
    min_entry_cash: float | None = None


@dataclass(frozen=True)
class ChargeConfigV2:
    brokerage_rate: float = 0.0
    stt_delivery_rate: float = 0.001
    exchange_txn_rate: float = 0.0000307
    sebi_rate: float = 0.000001
    stamp_buy_rate: float = 0.00015
    gst_rate: float = 0.18
    dp_sell_charge: float = 15.34


def _round_rupee(value: float) -> float:
    return float(int(value + 0.5))


def delivery_charges_v2(turnover: float, side: str, charges: ChargeConfigV2) -> float:
    brokerage = turnover * charges.brokerage_rate
    stt = _round_rupee(turnover * charges.stt_delivery_rate)
    exchange = turnover * charges.exchange_txn_rate
    sebi = turnover * charges.sebi_rate
    gst = (brokerage + exchange + sebi) * charges.gst_rate
    stamp = turnover * charges.stamp_buy_rate if side == "BUY" else 0.0
    dp = charges.dp_sell_charge if side == "SELL" else 0.0
    return brokerage + stt + exchange + sebi + gst + stamp + dp


# ---------- Indicator helpers ----------

def _compute_macd_hist(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    """MACD histogram: (EMA_fast − EMA_slow) − EMA_signal of that difference."""
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd - sig


def _wilder_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _wilder_atr(group: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR(14) per symbol. group has columns high, low, daily_close, sorted by date."""
    prev_close = group["daily_close"].shift(1)
    tr = pd.concat([
        (group["high"] - group["low"]),
        (group["high"] - prev_close).abs(),
        (group["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


_FEATURES_CACHE: "dict[tuple, pd.DataFrame]" = {}


def daily_features(prices: pd.DataFrame, scan_time: str, extra_scan_times: tuple[str, ...] = ()) -> pd.DataFrame:
    """Return per (symbol, date) features: prev_close, rsi14, volume_avg20, scan_volume_avg20, etc.

    `scan_volume` / `scan_volume_avg20` are computed for `scan_time` (the primary window) and
    aliased as-is for backward compat.  For each time in `extra_scan_times` (e.g. "14:00") an
    additional pair of columns is added: scan_volume_HHMM / scan_volume_avg20_HHMM so the engine
    can use calibrated volume ratios at alternate scan windows.

    Results are memoized per process: feature computation depends only on the price slice and
    the scan windows, so running many strategies over the same period reuses one computation.
    """
    if len(prices):
        _key = (
            len(prices),
            prices["timestamp"].iloc[0],
            prices["timestamp"].iloc[-1],
            float(prices["close"].iloc[0]),
            float(prices["close"].iloc[-1]),
            scan_time,
            tuple(extra_scan_times),
        )
        _cached = _FEATURES_CACHE.get(_key)
        if _cached is not None:
            return _cached
    else:
        _key = None

    daily_close = (
        prices.sort_values("timestamp")
        .groupby(["symbol", "date"], as_index=False)
        .tail(1)[["symbol", "date", "close", "high", "low"]]
        .rename(columns={"close": "daily_close"})
        .sort_values(["symbol", "date"])
        .reset_index(drop=True)
    )
    daily_close["prev_close"] = daily_close.groupby("symbol")["daily_close"].shift(1)
    for n in (2, 3, 5):
        daily_close[f"close_{n}d_ago"] = daily_close.groupby("symbol")["daily_close"].shift(n)
    daily_close["rsi14"] = (
        daily_close.groupby("symbol")["daily_close"].transform(lambda s: _wilder_rsi(s, 14))
    )
    # === paper-trading patch ============================================
    # Upstream uses groupby(...).apply(_wilder_atr) which raises ValueError on
    # pandas >= 2.2 ("Cannot set a DataFrame with multiple columns to the
    # single column atr14"). Recompute via transform — mathematically identical
    # to _wilder_atr but uses per-symbol aligned ops so pandas keeps a Series.
    _prev = daily_close.groupby("symbol")["daily_close"].shift(1)
    _tr = pd.concat(
        [
            daily_close["high"] - daily_close["low"],
            (daily_close["high"] - _prev).abs(),
            (daily_close["low"] - _prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    daily_close["atr14"] = _tr.groupby(daily_close["symbol"]).transform(
        lambda s: s.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    )
    # === end patch ======================================================
    sma20 = daily_close.groupby("symbol")["daily_close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    sd20 = daily_close.groupby("symbol")["daily_close"].transform(lambda s: s.rolling(20, min_periods=20).std())
    daily_close["bb_mid20"] = sma20
    daily_close["bb_lower20"] = sma20 - 2 * sd20
    daily_close["bb_upper20"] = sma20 + 2 * sd20
    daily_close["sma10"] = daily_close.groupby("symbol")["daily_close"].transform(
        lambda s: s.shift(1).rolling(10, min_periods=10).mean()
    )
    daily_close["sma50"] = daily_close.groupby("symbol")["daily_close"].transform(
        lambda s: s.shift(1).rolling(50, min_periods=50).mean()
    )
    # 90-day low using yesterday's bar back (shift 1) to avoid same-day lookahead.
    daily_close["low_90d"] = (
        daily_close.groupby("symbol")["low"]
        .transform(lambda s: s.shift(1).rolling(90, min_periods=20).min())
    )
    # 90-day high — same look-back, no same-day lookahead.
    # Used to require that a stock has already corrected meaningfully from its recent peak.
    daily_close["high_90d"] = (
        daily_close.groupby("symbol")["high"]
        .transform(lambda s: s.shift(1).rolling(90, min_periods=20).max())
    )
    # MACD histogram (12-26-9 standard) and a 3-day-lagged copy for "rising" filter.
    daily_close["macd_hist"] = (
        daily_close.groupby("symbol")["daily_close"]
        .transform(lambda s: _compute_macd_hist(s, fast=12, slow=26, signal=9))
    )
    daily_close["macd_hist_3d_ago"] = daily_close.groupby("symbol")["macd_hist"].shift(3)
    daily_close["macd_hist_20d_ago"] = daily_close.groupby("symbol")["macd_hist"].shift(20)

    # Scan-window volume (cumulative volume from market open to scan_time per day per symbol)
    scan_window = prices[prices["time"] <= scan_time]
    scan_vol = (
        scan_window.groupby(["symbol", "date"], as_index=False)["volume"].sum()
        .rename(columns={"volume": "scan_volume"})
    )
    scan_vol = scan_vol.sort_values(["symbol", "date"]).reset_index(drop=True)
    scan_vol["scan_volume_avg20"] = scan_vol.groupby("symbol")["scan_volume"].transform(
        lambda s: s.shift(1).rolling(20, min_periods=5).mean()
    )
    daily_close = daily_close.merge(scan_vol[["symbol", "date", "scan_volume", "scan_volume_avg20"]], on=["symbol", "date"], how="left")

    # Extra scan-time volumes (e.g. 14:00 for a dual-scan strategy)
    for est in extra_scan_times:
        est_key = est.replace(":", "")
        est_window = prices[prices["time"] <= est]
        est_sv = (
            est_window.groupby(["symbol", "date"], as_index=False)["volume"].sum()
            .rename(columns={"volume": f"scan_volume_{est_key}"})
        )
        est_sv = est_sv.sort_values(["symbol", "date"]).reset_index(drop=True)
        est_sv[f"scan_volume_avg20_{est_key}"] = est_sv.groupby("symbol")[f"scan_volume_{est_key}"].transform(
            lambda s: s.shift(1).rolling(20, min_periods=5).mean()
        )
        daily_close = daily_close.merge(
            est_sv[["symbol", "date", f"scan_volume_{est_key}", f"scan_volume_avg20_{est_key}"]],
            on=["symbol", "date"], how="left",
        )

    # Mean-reversion half-life (60-day rolling AR(1) on daily returns).
    # halflife = -ln(2) / ln(|phi|) where phi = lag-1 autocorrelation of daily returns.
    # Faster-reverting stocks (small halflife) give sharper entry-to-recovery cycles.
    # Used by mr_halflife_alloc_boost to bias allocation toward fast reverters.
    def _halflife_series(s: pd.Series) -> pd.Series:
        rets = s.pct_change()
        def _hl(window):
            if len(window) < 10:
                return float("nan")
            phi = window.autocorr(lag=1)
            if phi is None or np.isnan(phi) or abs(phi) <= 0 or abs(phi) >= 1:
                return float("nan")
            return -np.log(2.0) / np.log(abs(phi))
        return rets.rolling(60, min_periods=20).apply(_hl, raw=False)

    daily_close["mr_halflife_60d"] = daily_close.groupby("symbol")["daily_close"].transform(_halflife_series)

    if _key is not None:
        if len(_FEATURES_CACHE) > 16:
            _FEATURES_CACHE.clear()
        _FEATURES_CACHE[_key] = daily_close
    return daily_close


# === paper-trading patch ============================================
# Upstream read NIFTY_50.csv / SENSEX.csv / NIFTY_50_extended.csv from disk.
# The live trader has no CSVs — it primes _REGIME_INDEX_CACHE from the DB via
# prime_regime_index() before each replay. These return an empty series when
# the index hasn't been primed (the caller falls back to the synthetic proxy).
def _load_regime_index(name: str) -> pd.Series:
    """Return the primed daily-close series for `name`, or empty if not primed."""
    return _REGIME_INDEX_CACHE.get(name, pd.Series(dtype=float))


def _load_nifty_extended_close() -> pd.Series:
    """No extended CSV on the trading rig — the DB's NIFTY_50 daily history
    (~1,200+ bars) is deep enough for a 50-DMA. Return the primed NIFTY_50 series."""
    return _REGIME_INDEX_CACHE.get("NIFTY_50", pd.Series(dtype=float))
# === end patch ======================================================


def regime_series(daily_features_df: pd.DataFrame, source: str | None = None, dma_period: int = 50) -> pd.Series:
    """Boolean Series indexed by date: True when the regime proxy is above its N-DMA.

    `source`:
      - None  -> synthetic equal-weight universe proxy from `daily_features_df` (back-compat).
      - "NIFTY_50" / "SENSEX" -> real index close from data/angel_symbols/{source}.csv.
    `dma_period`: lookback for the moving average threshold (default 50).

    For NIFTY_50 with dma_period > 50, prefers NIFTY_50_extended.csv (starts 2018) so
    long-period DMA (e.g. 200-DMA) has sufficient warmup history before the backtest window.
    Critical: the rolling DMA is computed on the FULL historical series before restricting to
    feature_dates, so historical context is not discarded before the DMA calculation.
    """
    feature_dates = set(daily_features_df["date"].unique())
    if source is None:
        pivot = daily_features_df.pivot_table(index="date", columns="symbol", values="daily_close")
        rebased = pivot.div(pivot.bfill().iloc[0]).mean(axis=1)
        min_p = 20 if dma_period <= 50 else max(20, dma_period // 5)
        dma = rebased.rolling(dma_period, min_periods=min_p).mean()
        bull = rebased > dma
        return bull.loc[bull.index.isin(feature_dates)]
    else:
        # For NIFTY_50 with long DMA, use the extended historical CSV (2018+) if available
        # so that e.g. 200-DMA in 2021-22 is computed from genuine 200 trading days of history.
        if source == "NIFTY_50" and dma_period > 50:
            index_close = _load_nifty_extended_close()
        else:
            index_close = _load_regime_index(source)
        if index_close.empty:
            # Fall back to synthetic if the file is missing so the strategy doesn't silently no-op.
            return regime_series(daily_features_df, source=None)
        # Compute DMA on FULL historical series (preserves warmup context), then restrict to
        # feature_dates. This ensures the 200-DMA at e.g. 2021-05-08 reflects genuine 200-day
        # history going back to 2020, not just the last 40 trading days of the short CSV.
        min_p = 20 if dma_period <= 50 else max(20, dma_period // 5)
        dma = index_close.rolling(dma_period, min_periods=min_p).mean()
        bull_full = (index_close > dma).astype(float)  # float avoids object-dtype NaN on reindex
        # Forward-fill any date gaps (weekends, holidays) then restrict to feature dates.
        all_dates = sorted(feature_dates.union(set(bull_full.index)))
        bull_reindexed = bull_full.reindex(all_dates).ffill().fillna(0.0).astype(bool)
        return bull_reindexed.loc[bull_reindexed.index.isin(feature_dates)]


def classify_regime_by_date(
    vix_bear_threshold: float | None = 20.0,
    vix_only_bear: bool = False,
) -> pd.Series:
    """Return a Series (date → 'bull' | 'bear' | 'sideways') using the extended NIFTY CSV.

    When vix_only_bear=False (default):
      1. If India VIX > vix_bear_threshold AND NIFTY < 50-DMA → 'bear'  (fear + breakdown)
      2. NIFTY close > 50-DMA AND 20-DMA > 50-DMA → 'bull'  (established uptrend)
      3. NIFTY close < 50-DMA AND 20-DMA < 50-DMA → 'bear'  (established downtrend)
      4. Everything else → 'sideways'

    When vix_only_bear=True:
      Bear is triggered ONLY by VIX > threshold (no DMA-based bear).
      This avoids misclassifying sharp-but-brief corrections as 'bear'
      (e.g., 2024-25: only 19 days with VIX>20 vs 82 with DMA bear).

    Results are cached by (vix_bear_threshold, vix_only_bear).
    """
    cache_key = f"regime_{vix_bear_threshold}_{vix_only_bear}"
    if cache_key in _REGIME_CACHE:
        return _REGIME_CACHE[cache_key]

    # === paper-trading patch ============================================
    # Upstream read NIFTY from NIFTY_50_extended.csv and VIX from
    # INDIA_VIX_extended.csv. The trader primes both into _REGIME_INDEX_CACHE
    # from the DB (NIFTY_50 + INDIA_VIX daily 1d closes) before each replay.
    close = _REGIME_INDEX_CACHE.get("NIFTY_50", pd.Series(dtype=float))
    if close.empty:
        return pd.Series(dtype=str)
    close = close.astype(float).sort_index()
    # === end patch ======================================================

    dma20 = close.rolling(20, min_periods=10).mean()
    dma50 = close.rolling(50, min_periods=20).mean()

    regime = pd.Series("sideways", index=close.index, dtype=str)
    bull_mask = (close > dma50) & (dma20 > dma50)
    regime[bull_mask] = "bull"

    if not vix_only_bear:
        # DMA-based bear classification
        bear_mask = (close < dma50) & (dma20 < dma50)
        regime[bear_mask] = "bear"

    # === paper-trading patch ============================================
    # VIX override: high fear → force bear (regardless of DMA). VIX daily
    # closes are primed from the DB under the key "INDIA_VIX".
    _vix = _REGIME_INDEX_CACHE.get("INDIA_VIX", pd.Series(dtype=float))
    if vix_bear_threshold is not None and not _vix.empty:
        vix_close = _vix.astype(float).sort_index().reindex(close.index).ffill()
        if vix_only_bear:
            vix_bear = vix_close > vix_bear_threshold
        else:
            vix_bear = (vix_close > vix_bear_threshold) & (close < dma50)
        regime[vix_bear.fillna(False)] = "bear"
    # === end patch ======================================================

    _REGIME_CACHE[cache_key] = regime
    return regime


# ---------- Position bookkeeping ----------

def _new_holding(
    qty: float,
    price: float,
    date,
    entry_atr: float | None = None,
    staged_pending: bool = False,
    staged_remaining_alloc: float = 0.0,
    entry_depth_pct: float | None = None,
) -> dict:
    return {
        "qty": float(qty),
        "avg_price": float(price),
        "entry_price": float(price),
        "entry_date": date,
        "peak_price": float(price),
        "pyramid_adds_hit": 0,
        "tiers_hit": 0,
        "trail_armed": False,
        "entry_atr": float(entry_atr) if entry_atr is not None and not np.isnan(entry_atr) else None,
        "entry_depth_pct": float(entry_depth_pct) if entry_depth_pct is not None and not np.isnan(entry_depth_pct) else None,
        "staged_pending": staged_pending,
        "staged_remaining_alloc": staged_remaining_alloc,
    }


def _select_adaptive_tiers(depth_pct: float | None, buckets):
    """Pick the LAST bucket whose min_depth ≤ depth_pct. Returns None if no match."""
    if depth_pct is None or not buckets:
        return None
    chosen = None
    for min_depth, tiers in buckets:
        if depth_pct >= min_depth:
            chosen = tiers
        else:
            break
    return chosen


def _add_to_holding(holding: dict, add_qty: float, add_price: float) -> None:
    total_qty = holding["qty"] + add_qty
    holding["avg_price"] = (holding["avg_price"] * holding["qty"] + add_price * add_qty) / total_qty
    holding["qty"] = total_qty


# === paper-trading patch ============================================
# Upstream read India VIX from INDIA_VIX_extended.csv. The trader primes the
# DB's INDIA_VIX daily closes into _REGIME_INDEX_CACHE before each replay; build
# the date→float map from there. Used only by vix_blend strategies (S404 does
# not enable vix_blend, so this is a no-op for it, but kept correct for others).
def _load_vix_by_date() -> dict:
    """Return India VIX close keyed by Python date, from the primed cache.

    Empty dict when INDIA_VIX hasn't been primed, so callers degrade gracefully.
    """
    global _VIX_SERIES_CACHE
    if _VIX_SERIES_CACHE is not None:
        return _VIX_SERIES_CACHE
    vix = _REGIME_INDEX_CACHE.get("INDIA_VIX", pd.Series(dtype=float))
    if vix.empty:
        _VIX_SERIES_CACHE = {}
        return {}
    vix_close = vix.astype(float).sort_index().ffill()
    result: dict = dict(zip(vix_close.index, vix_close.values))
    _VIX_SERIES_CACHE = result
    return result
# === end patch ======================================================


# ---------- Engine ----------

def run_backtest_v2(
    prices: pd.DataFrame,
    strategy: StrategyV2,
    charges: ChargeConfigV2,
    start_date=None,
    deposits: "dict[str, float] | None" = None,
    external_positions: "dict[str, dict] | None" = None,
    cash_override: "dict[str, float] | None" = None,
) -> dict:
    """Run V2 backtest. `prices` must have columns: timestamp, symbol, open, high, low, close, volume, date, time.

    Args:
        deposits: Optional dict mapping date strings ("YYYY-MM-DD") to cash amounts.
            On matching trading days, the amount is injected into `cash` before exits/entries.
            Enables SIP-style variable-deposit scenarios where capital is added over time.
            The first deposit is typically handled via strategy.starting_cash; subsequent ones
            go here.  When pct_equity allocation mode is used, each deposit automatically scales
            future position sizes upward — no strategy parameter changes needed.
        external_positions: Optional map of broker positions the engine did NOT create but should
            still MANAGE — i.e. adopt and exit per the strategy's rules. Shape:
            ``{"YYYY-MM-DD": {"RELIANCE": {"qty": int, "avg_price": float}, ...}}``. On (or after)
            each keyed date, the symbol is injected into `holdings` (if not already held) with the
            broker avg as entry, and `entry_depth_pct` / `entry_atr` snapshot from that day's
            features so the adaptive exit ladder applies exactly as it would for a native entry.
            Injection cost (`qty * avg_price`) is debited from `cash` to conserve equity. The
            engine's own entry guard (`if symbol in holdings`) then prevents re-buying it. Used by
            the live real-money trader to absorb manual buys / orphaned fills; ``None`` (default)
            on every backtest and paper path → behaviour byte-for-byte unchanged.
        cash_override: Optional map of date strings ("YYYY-MM-DD") to an absolute cash value
            that *replaces* the simulated ``cash`` at the top of that day (after any deposit /
            adoption injection — it is the final word). The live trader passes
            ``{today: broker_free_cash}`` so entry sizing (pct_equity allocation + the
            ``min_entry_cash`` gate) reflects the account's REAL free cash, absorbing manual
            sells / withdrawals the stateless replay can't otherwise see. ``None`` (default) on
            every backtest and paper path → behaviour byte-for-byte unchanged.

    Returns dict with summary, equity_curve, trades, open_positions, deposits log, the
    external-injection log, and the cash-override log.
    """
    # Determine effective scan windows for entry (primary + any extras)
    _effective_scan_times: tuple[str, ...] = strategy.scan_times if strategy.scan_times is not None else (strategy.scan_time,)
    _primary_scan_time = _effective_scan_times[0]
    _extra_scan_times = _effective_scan_times[1:]

    features = daily_features(prices, _primary_scan_time, _extra_scan_times)

    # Build the column list for the merge — include extra volume columns when present
    _feature_cols = [
        "symbol", "date",
        "prev_close", "close_2d_ago", "close_3d_ago", "close_5d_ago",
        "rsi14", "atr14",
        "bb_mid20", "bb_lower20", "bb_upper20",
        "sma10", "sma50",
        "low_90d", "high_90d",
        "macd_hist", "macd_hist_3d_ago", "macd_hist_20d_ago",
        "scan_volume", "scan_volume_avg20",
        "mr_halflife_60d",  # rolling AR(1) half-life; used by mr_halflife_alloc_boost
    ]
    for _est in _extra_scan_times:
        _est_key = _est.replace(":", "")
        _feature_cols += [f"scan_volume_{_est_key}", f"scan_volume_avg20_{_est_key}"]

    prices = prices.merge(
        features[_feature_cols],
        on=["symbol", "date"],
        how="left",
    )

    regime_ok_by_date = (
        regime_series(features, source=strategy.regime_source, dma_period=strategy.regime_dma_period)
        if strategy.regime_filter
        else None
    )

    # Multi-regime classifier (bull / bear / sideways).  Only computed when mode_params are set.
    _use_multiregime = (
        strategy.mode_params_bull is not None
        or strategy.mode_params_bear is not None
        or strategy.mode_params_sideways is not None
    )
    mode_by_date: pd.Series | None = None
    if _use_multiregime:
        mode_by_date = classify_regime_by_date(
            vix_bear_threshold=strategy.vix_bear_threshold,
            vix_only_bear=strategy.vix_only_bear,
        )

    # Regime series used for conditional MACD and SMA filtering (legacy path).
    bear_market_by_date: pd.Series | None = None
    _need_regime = (
        (strategy.macd_filter is not None and strategy.macd_filter_in_bear_market)
        or (strategy.sma_above_prev is not None and strategy.sma_above_prev_in_bear)
        or (strategy.exit_tiers_bear is not None)
    )
    if _need_regime:
        src = strategy.regime_source or "NIFTY_50"
        bear_market_by_date = regime_series(features, source=src, dma_period=strategy.regime_dma_period)
        # bear_market_by_date: True = bullish, False = bearish (inverted below at point-of-use)

    # NIFTY N-day momentum filter: skip new entries during rapid index corrections.
    nifty_momentum_by_date: dict = {}
    if strategy.nifty_momentum_filter is not None:
        _nifty = _load_regime_index("NIFTY_50")
        if not _nifty.empty:
            _nifty_ret = _nifty.pct_change(strategy.nifty_momentum_lookback)
            nifty_momentum_by_date = _nifty_ret.to_dict()

    if start_date:
        start = pd.to_datetime(start_date).date()
        prices = prices[prices["date"] >= start]

    cash = float(strategy.starting_cash)
    holdings: dict[str, dict] = {}
    trades: list[dict] = []
    equity_curve: list[dict] = []
    _deposit_log: list[dict] = []   # records each injected deposit for SIP reporting

    # External-position adoption: flatten the date-keyed map to one earliest injection
    # date per symbol, so a position whose first-seen date falls on a non-trading day (or
    # before the window) still gets injected on the first eligible trading day rather than
    # being silently dropped. `_ext_injected` guards against re-injecting after a later exit.
    _ext_seed: dict[str, dict] = {}
    if external_positions:
        for _dstr, _syms in external_positions.items():
            try:
                _dobj = pd.to_datetime(_dstr).date()
            except (TypeError, ValueError):
                continue
            for _sym, _info in (_syms or {}).items():
                try:
                    _seed = {"date": _dobj, "qty": int(_info["qty"]), "avg_price": float(_info["avg_price"])}
                except (TypeError, ValueError, KeyError):
                    continue
                if _seed["qty"] <= 0 or _seed["avg_price"] <= 0:
                    continue
                cur = _ext_seed.get(_sym)
                if cur is None or _seed["date"] < cur["date"]:
                    _ext_seed[_sym] = _seed
    _ext_injected: set[str] = set()
    _external_log: list[dict] = []  # records each adopted position for /bot visibility
    _cash_override_log: list[dict] = []  # records each broker cash mark — empty when none

    trading_day_idx: dict = {}  # date -> index for time stops
    sorted_days = sorted(prices["date"].unique())
    for i, d in enumerate(sorted_days):
        trading_day_idx[d] = i

    for day, day_prices in prices.groupby("date", sort=True):
        # ---- SIP deposit injection ----
        # Cash is injected BEFORE exits/entries so the new capital can be deployed same day.
        if deposits:
            _dep = deposits.get(str(day), 0.0)
            if _dep > 0.0:
                cash += _dep
                _deposit_log.append({
                    "date": str(day),
                    "amount": round(_dep, 2),
                    "cash_after": round(cash, 2),
                })

        # ---- External-position adoption ----
        # Inject any broker position due on/before today that the engine isn't already
        # holding, BEFORE the exit/entry passes so it (a) can't be re-bought by the entry
        # guard and (b) is eligible for a same-day tier exit if already above target.
        # entry_depth_pct / entry_atr are snapshot from this day's features so the S404
        # adaptive ladder picks the same bucket it would for a native entry.
        if _ext_seed:
            for _sym, _info in _ext_seed.items():
                if _sym in _ext_injected or _sym in holdings or _info["date"] > day:
                    continue
                _srow = day_prices[day_prices["symbol"] == _sym]
                _entry_atr = None
                _entry_depth = None
                if not _srow.empty:
                    _r0 = _srow.iloc[0]
                    _atr = _r0.get("atr14")
                    _entry_atr = float(_atr) if _atr is not None and not pd.isna(_atr) else None
                    _h90 = _r0.get("high_90d")
                    if _h90 is not None and not pd.isna(_h90) and float(_h90) > 0:
                        _entry_depth = (float(_h90) - _info["avg_price"]) / float(_h90) * 100.0
                holdings[_sym] = _new_holding(
                    _info["qty"], _info["avg_price"], day, _entry_atr,
                    entry_depth_pct=_entry_depth,
                )
                cash -= _info["qty"] * _info["avg_price"]  # conserve equity: capital already spent
                _ext_injected.add(_sym)
                _external_log.append({
                    "date": str(day), "symbol": _sym,
                    "qty": _info["qty"], "avg_price": round(_info["avg_price"], 2),
                    "entry_depth_pct": round(_entry_depth, 2) if _entry_depth is not None else None,
                    "cash_after": round(cash, 2),
                })

        # ---- Cash mark-to-broker ----
        # Override simulated cash with the account's REAL free cash for this day so entry
        # sizing (pct_equity allocation + the min_entry_cash gate) reflects the true account,
        # absorbing manual sells / withdrawals the stateless replay can't see. Applied AFTER
        # the deposit + adoption injections — it is the final word, since broker free cash
        # already nets out money spent on adopted/manual positions. None (every backtest /
        # paper path) → no-op, parity preserved.
        if cash_override:
            _ov = cash_override.get(str(day))
            if _ov is not None:
                cash = float(_ov)
                _cash_override_log.append({"date": str(day), "cash": round(cash, 2)})

        day_prices = day_prices.sort_values("timestamp")
        day_idx = trading_day_idx[day]

        # ---- Resolve per-day mode params (multi-regime switching) ----
        _day_mode: ModeParams | None = None
        if mode_by_date is not None:
            _mregime = mode_by_date.get(day, "sideways")
            if _mregime == "bull":
                _day_mode = strategy.mode_params_bull
            elif _mregime == "bear":
                _day_mode = strategy.mode_params_bear
            else:
                _day_mode = strategy.mode_params_sideways
        # Effective per-day parameters (fall back to base strategy if mode param is None)
        _eff_fall    = _meff(_day_mode, "fall_threshold",      strategy.fall_threshold)
        _eff_alloc   = _meff(_day_mode, "allocation_pct",      strategy.allocation_pct)
        _eff_exits   = _meff(_day_mode, "exit_tiers",          strategy.exit_tiers)
        _eff_vol     = _meff(_day_mode, "volume_spike_min",     strategy.volume_spike_min)
        _eff_macd    = _meff(_day_mode, "macd_filter",         strategy.macd_filter)
        _eff_sma_p   = _meff(_day_mode, "sma_above_prev",      strategy.sma_above_prev)
        _eff_pyrlvl  = _meff(_day_mode, "pyramid_levels",      strategy.pyramid_levels)
        _eff_maxbuys = _meff(_day_mode, "max_new_buys_per_day", strategy.max_new_buys_per_day)
        # "__off__" sentinel: explicit disable in a mode (overrides base strategy value)
        if _eff_macd   == "__off__": _eff_macd   = None
        if _eff_sma_p  == -1:       _eff_sma_p  = None  # -1 used to turn off sma_above_prev

        # ---- VIX blend factor for adaptive exits ----
        # Computed once per day; applied to adaptive tier thresholds at point-of-use.
        # factor = clamp(1 + (vix - baseline) * slope, lo, hi)
        # Default 1.0 (no scaling) when vix_blend is off or VIX data unavailable.
        _vix_factor: float = 1.0
        if strategy.vix_blend_enabled and strategy.adaptive_exit_by_depth is not None:
            if not hasattr(run_backtest_v2, "_vix_by_date_cache"):
                run_backtest_v2._vix_by_date_cache = _load_vix_by_date()  # type: ignore[attr-defined]
            _vix_today = run_backtest_v2._vix_by_date_cache.get(day)  # type: ignore[attr-defined]
            if _vix_today is not None:
                raw = 1.0 + (_vix_today - strategy.vix_blend_baseline) * strategy.vix_blend_slope
                _vix_factor = max(strategy.vix_blend_clamp_lo, min(strategy.vix_blend_clamp_hi, raw))

        # ---- Exit pass: stops + tiered targets ----
        for symbol in list(holdings):
            position = holdings[symbol]
            symbol_day = day_prices[day_prices["symbol"] == symbol]
            if symbol_day.empty:
                continue

            day_high = float(symbol_day["high"].max())
            day_low = float(symbol_day["low"].min())
            day_close = float(symbol_day.iloc[-1]["close"])
            position["peak_price"] = max(position["peak_price"], day_high)

            avg = position["avg_price"]
            sold_full = False

            # 1) Hard stop on intraday low (fixed % or ATR-based)
            stop_price = None
            stop_reason = None
            if strategy.atr_stop_multiplier is not None and position.get("entry_atr"):
                stop_price = avg - strategy.atr_stop_multiplier * position["entry_atr"]
                stop_reason = f"atr_stop_{strategy.atr_stop_multiplier:g}xATR"
            elif strategy.hard_stop_pct is not None:
                stop_price = avg * (1 + strategy.hard_stop_pct)
                stop_reason = f"hard_stop_{strategy.hard_stop_pct:+.0%}"
            if stop_price is not None and day_low <= stop_price:
                qty = position["qty"]
                sell_price = stop_price * (1 - strategy.slippage_rate)
                turnover = qty * sell_price
                fee = delivery_charges_v2(turnover, "SELL", charges)
                cash += turnover - fee
                trades.append({
                    "date": str(day),
                    "time": symbol_day.iloc[0]["timestamp"].strftime("%H:%M"),
                    "symbol": symbol, "side": "SELL", "qty": int(qty),
                    "price": round(sell_price, 2), "turnover": round(turnover, 2),
                    "charges": round(fee, 2), "cash_after": round(cash, 2),
                    "reason": stop_reason,
                })
                del holdings[symbol]
                sold_full = True

            if sold_full:
                continue

            # 2) Time stop
            if strategy.time_stop_days is not None:
                held_days = day_idx - trading_day_idx.get(position["entry_date"], day_idx)
                if held_days >= strategy.time_stop_days and day_close < avg:
                    qty = position["qty"]
                    sell_price = day_close * (1 - strategy.slippage_rate)
                    turnover = qty * sell_price
                    fee = delivery_charges_v2(turnover, "SELL", charges)
                    cash += turnover - fee
                    trades.append({
                        "date": str(day),
                        "time": symbol_day.iloc[-1]["timestamp"].strftime("%H:%M"),
                        "symbol": symbol, "side": "SELL", "qty": int(qty),
                        "price": round(sell_price, 2), "turnover": round(turnover, 2),
                        "charges": round(fee, 2), "cash_after": round(cash, 2),
                        "reason": f"time_stop_{strategy.time_stop_days}d",
                    })
                    del holdings[symbol]
                    continue

            # 3) Tiered profit exits — process tiers in order
            # Priority:
            #   mode.adaptive_exit_by_depth (per-regime per-position)
            #   > strategy.adaptive_exit_by_depth (global per-position)
            #   > multi-regime _eff_exits
            #   > legacy exit_tiers_bear
            #   > base exit_tiers
            _mode_adaptive_buckets = (
                _day_mode.adaptive_exit_by_depth
                if _day_mode is not None and _day_mode.adaptive_exit_by_depth is not None
                else None
            )
            _adaptive_buckets = _mode_adaptive_buckets if _mode_adaptive_buckets is not None else strategy.adaptive_exit_by_depth
            _adaptive = (
                _select_adaptive_tiers(position.get("entry_depth_pct"), _adaptive_buckets)
                if _adaptive_buckets is not None else None
            )
            if _adaptive is not None:
                # Apply VIX blend factor: scale every tier threshold by _vix_factor.
                if _vix_factor != 1.0:
                    _adaptive = tuple((p * _vix_factor, f) for p, f in _adaptive)
                tiers = _adaptive
            elif _day_mode is not None and _day_mode.exit_tiers is not None:
                tiers = _eff_exits
            elif strategy.exit_tiers_bear is not None and bear_market_by_date is not None:
                _is_bear = not bool(bear_market_by_date.get(day, True))
                tiers = strategy.exit_tiers_bear if _is_bear else strategy.exit_tiers
            else:
                tiers = strategy.exit_tiers
            tier_idx = position["tiers_hit"]
            while tier_idx < len(tiers) and not sold_full:
                profit_pct, frac = tiers[tier_idx]
                target = avg * (1 + profit_pct)
                if day_high < target:
                    break
                qty_to_sell = position["qty"] if (tier_idx == len(tiers) - 1 or frac >= 1.0) else max(1.0, position["qty"] * frac)
                qty_to_sell = float(int(qty_to_sell))
                if qty_to_sell <= 0:
                    break
                sell_price = target * (1 - strategy.slippage_rate)
                turnover = qty_to_sell * sell_price
                fee = delivery_charges_v2(turnover, "SELL", charges)
                cash += turnover - fee
                trades.append({
                    "date": str(day),
                    "time": symbol_day.iloc[0]["timestamp"].strftime("%H:%M"),
                    "symbol": symbol, "side": "SELL", "qty": int(qty_to_sell),
                    "price": round(sell_price, 2), "turnover": round(turnover, 2),
                    "charges": round(fee, 2), "cash_after": round(cash, 2),
                    "reason": f"target_{profit_pct:+.0%}_tier{tier_idx + 1}",
                })
                position["qty"] -= qty_to_sell
                position["tiers_hit"] += 1
                tier_idx += 1
                if position["qty"] <= 0:
                    del holdings[symbol]
                    sold_full = True

            if sold_full:
                continue

            # 4) Trailing stop (if armed)
            if strategy.trail_activate_pct is not None and strategy.trail_drawdown_pct is not None:
                if not position["trail_armed"]:
                    if day_high >= avg * (1 + strategy.trail_activate_pct):
                        position["trail_armed"] = True
                if position["trail_armed"]:
                    trail_price = position["peak_price"] * (1 - strategy.trail_drawdown_pct)
                    if day_low <= trail_price:
                        qty = position["qty"]
                        sell_price = trail_price * (1 - strategy.slippage_rate)
                        turnover = qty * sell_price
                        fee = delivery_charges_v2(turnover, "SELL", charges)
                        cash += turnover - fee
                        trades.append({
                            "date": str(day),
                            "time": symbol_day.iloc[-1]["timestamp"].strftime("%H:%M"),
                            "symbol": symbol, "side": "SELL", "qty": int(qty),
                            "price": round(sell_price, 2), "turnover": round(turnover, 2),
                            "charges": round(fee, 2), "cash_after": round(cash, 2),
                            "reason": f"trail_stop_{strategy.trail_drawdown_pct:.0%}",
                        })
                        del holdings[symbol]

            # 5) Patience sell: profitable but stalled positions recycled after N days.
            #    Only fires when unrealized_return >= patience_sell_min_profit AND
            #    we are still below the NEXT un-hit tier target (otherwise tier logic fires).
            #    Never fires on losing positions → no re-entry-loop risk.
            if symbol in holdings and strategy.patience_sell_after_days is not None:
                position = holdings[symbol]
                held_days_count = day_idx - trading_day_idx.get(position["entry_date"], day_idx)
                if held_days_count >= strategy.patience_sell_after_days:
                    unrealized = day_close / position["avg_price"] - 1
                    if unrealized >= strategy.patience_sell_min_profit:
                        # Only fire if position hasn't exited yet (sold_full check is embedded via
                        # `symbol in holdings`) and hasn't reached next tier on this very day.
                        tier_idx = position["tiers_hit"]
                        _mode_adaptive_buckets_p = (
                            _day_mode.adaptive_exit_by_depth
                            if _day_mode is not None and _day_mode.adaptive_exit_by_depth is not None
                            else None
                        )
                        _adaptive_buckets_p = _mode_adaptive_buckets_p if _mode_adaptive_buckets_p is not None else strategy.adaptive_exit_by_depth
                        _adaptive_p = (
                            _select_adaptive_tiers(position.get("entry_depth_pct"), _adaptive_buckets_p)
                            if _adaptive_buckets_p is not None else None
                        )
                        if _adaptive_p is not None:
                            if _vix_factor != 1.0:
                                _adaptive_p = tuple((p * _vix_factor, f) for p, f in _adaptive_p)
                            tiers = _adaptive_p
                        elif strategy.exit_tiers_bear is not None and bear_market_by_date is not None:
                            _is_bear_p = not bool(bear_market_by_date.get(day, True))
                            tiers = strategy.exit_tiers_bear if _is_bear_p else strategy.exit_tiers
                        else:
                            tiers = strategy.exit_tiers
                        next_tier_pct = tiers[tier_idx][0] if tier_idx < len(tiers) else 1e9
                        if unrealized < next_tier_pct:
                            qty = position["qty"]
                            sell_price = day_close * (1 - strategy.slippage_rate)
                            turnover = qty * sell_price
                            fee = delivery_charges_v2(turnover, "SELL", charges)
                            cash += turnover - fee
                            trades.append({
                                "date": str(day),
                                "time": symbol_day.iloc[-1]["timestamp"].strftime("%H:%M"),
                                "symbol": symbol, "side": "SELL", "qty": int(qty),
                                "price": round(sell_price, 2), "turnover": round(turnover, 2),
                                "charges": round(fee, 2), "cash_after": round(cash, 2),
                                "reason": (
                                    f"patience_{strategy.patience_sell_after_days}d"
                                    f"_min{strategy.patience_sell_min_profit:.0%}"
                                ),
                            })
                            del holdings[symbol]

        # ---- Pyramid add pass: existing holdings only ----
        for symbol in list(holdings):
            position = holdings[symbol]

            # Staged-entry second tranche (fires once, the trading day after initial entry)
            if strategy.staged_entry and position.get("staged_pending", False):
                symbol_day_s = day_prices[day_prices["symbol"] == symbol]
                if not symbol_day_s.empty:
                    scan_rows_s = symbol_day_s[symbol_day_s["time"] <= strategy.scan_time]
                    if not scan_rows_s.empty:
                        scan_row_s = scan_rows_s.iloc[-1]
                        current_price_s = float(scan_row_s["close"])
                        # Only add if price hasn't bounced above the original entry price
                        if current_price_s <= position["entry_price"]:
                            staged_alloc = position.get("staged_remaining_alloc", 0.0)
                            buy_price_s = current_price_s * (1 + strategy.slippage_rate)
                            qty_s = int(staged_alloc // buy_price_s)
                            if qty_s > 0:
                                turnover_s = qty_s * buy_price_s
                                fee_s = delivery_charges_v2(turnover_s, "BUY", charges)
                                if cash >= turnover_s + fee_s:
                                    cash -= turnover_s + fee_s
                                    _add_to_holding(position, qty_s, buy_price_s)
                                    trades.append({
                                        "date": str(day),
                                        "time": scan_row_s["timestamp"].strftime("%H:%M"),
                                        "symbol": symbol, "side": "BUY", "qty": int(qty_s),
                                        "price": round(buy_price_s, 2), "turnover": round(turnover_s, 2),
                                        "charges": round(fee_s, 2), "cash_after": round(cash, 2),
                                        "reason": "staged_entry_2nd_tranche",
                                    })
                # Always clear staged_pending after the day (whether or not we could add)
                position["staged_pending"] = False

            _active_pyrlvl = _eff_pyrlvl if _eff_pyrlvl is not None else strategy.pyramid_levels
            if position["pyramid_adds_hit"] >= len(_active_pyrlvl):
                continue
            symbol_day = day_prices[day_prices["symbol"] == symbol]
            if symbol_day.empty:
                continue
            scan_rows = symbol_day[symbol_day["time"] <= strategy.scan_time]
            if scan_rows.empty:
                continue
            scan_row = scan_rows.iloc[-1]
            current_price = float(scan_row["close"])
            basis = position["avg_price"] if strategy.pyramid_basis == "avg" else position["entry_price"]
            level_idx = position["pyramid_adds_hit"]
            drop_pct, alloc_param = _active_pyrlvl[level_idx]
            if strategy.pyramid_volume_filter and _eff_vol is not None:
                vol = scan_row.get("scan_volume")
                vol_avg = scan_row.get("scan_volume_avg20")
                if vol is None or vol_avg is None or pd.isna(vol_avg) or vol < _eff_vol * vol_avg:
                    continue
            trigger = basis * (1 + drop_pct)
            # Take the add only if drop is realized and we still have capital
            if current_price > trigger:
                continue
            # Resolve pyramid allocation: in fixed mode alloc_param is rupees; in pct_cash/pct_equity it is a fraction.
            if strategy.allocation_mode == "pct_cash":
                alloc = max(cash * alloc_param, 0.0)
            elif strategy.allocation_mode == "pct_equity":
                marks_today = day_prices.sort_values("timestamp").groupby("symbol").tail(1).set_index("symbol")["close"]
                holdings_value_now = sum(p["qty"] * marks_today.get(s, p["avg_price"]) for s, p in holdings.items())
                alloc = max((cash + holdings_value_now) * alloc_param, 0.0)
            else:
                alloc = alloc_param
            buy_price = current_price * (1 + strategy.slippage_rate)
            qty = int(alloc // buy_price)
            if qty <= 0:
                position["pyramid_adds_hit"] += 1
                continue
            turnover = qty * buy_price
            fee = delivery_charges_v2(turnover, "BUY", charges)
            if cash < turnover + fee:
                position["pyramid_adds_hit"] += 1
                continue
            cash -= turnover + fee
            _add_to_holding(position, qty, buy_price)
            position["pyramid_adds_hit"] += 1
            trades.append({
                "date": str(day),
                "time": scan_row["timestamp"].strftime("%H:%M"),
                "symbol": symbol, "side": "BUY", "qty": int(qty),
                "price": round(buy_price, 2), "turnover": round(turnover, 2),
                "charges": round(fee, 2), "cash_after": round(cash, 2),
                "reason": f"pyramid_{strategy.pyramid_basis}_{drop_pct:+.0%}_lvl{level_idx + 1}",
            })

        # ---- New entry pass (iterates over each configured scan window) ----
        regime_pass = True
        if strategy.regime_filter and regime_ok_by_date is not None:
            regime_pass = bool(regime_ok_by_date.get(day, False))
        # NIFTY momentum gate: block new entries when index is in rapid correction.
        if regime_pass and strategy.nifty_momentum_filter is not None:
            nifty_ret_today = nifty_momentum_by_date.get(day, None)
            if nifty_ret_today is not None and not pd.isna(nifty_ret_today):
                if nifty_ret_today < strategy.nifty_momentum_filter:
                    regime_pass = False

        if regime_pass:
            # Iterate over each scan window (e.g. 11:00 and 14:00 for dual-scan strategies).
            # Trigger mode ignores scan_times and runs once (the intraday candle walk is its own loop).
            _scan_windows = _effective_scan_times if strategy.entry_mode == "scan" else (_primary_scan_time,)

            for _scan_t in _scan_windows:
                _vol_key = _scan_t.replace(":", "")
                _vol_col = "scan_volume" if _scan_t == _primary_scan_time else f"scan_volume_{_vol_key}"
                _vol_avg_col = "scan_volume_avg20" if _scan_t == _primary_scan_time else f"scan_volume_avg20_{_vol_key}"

                candidates = None

                if strategy.entry_mode == "scan":
                    scan_rows = (
                        day_prices[day_prices["time"] <= _scan_t]
                        .sort_values("timestamp")
                        .groupby("symbol", as_index=False)
                        .tail(1)
                    )
                    if not scan_rows.empty:
                        if strategy.entry_lookback_days <= 1:
                            scan_rows = scan_rows.dropna(subset=["prev_close"]).copy()
                            scan_rows["change"] = scan_rows["close"] / scan_rows["prev_close"] - 1
                        else:
                            col = f"close_{strategy.entry_lookback_days}d_ago"
                            if col not in scan_rows.columns:
                                scan_rows = scan_rows.copy()
                                scan_rows["change"] = np.nan
                            else:
                                scan_rows = scan_rows.dropna(subset=[col]).copy()
                                scan_rows["change"] = scan_rows["close"] / scan_rows[col] - 1

                        candidates = scan_rows[scan_rows["change"] <= _eff_fall].copy()
                        if _eff_vol is not None and _vol_avg_col in candidates.columns:
                            candidates = candidates[
                                candidates[_vol_col]
                                >= _eff_vol * candidates[_vol_avg_col]
                            ]

                elif strategy.entry_mode == "trigger":
                    # Walk intraday candles within trigger_window; first cross per (symbol, date) wins.
                    t_lo, t_hi = strategy.trigger_window
                    window_rows = day_prices[
                        (day_prices["time"] >= t_lo) & (day_prices["time"] <= t_hi)
                    ].copy()
                    if not window_rows.empty:
                        window_rows = window_rows.dropna(subset=["prev_close"])
                        if strategy.entry_lookback_days <= 1:
                            window_rows["change"] = window_rows["close"] / window_rows["prev_close"] - 1
                        else:
                            col = f"close_{strategy.entry_lookback_days}d_ago"
                            if col in window_rows.columns:
                                window_rows = window_rows.dropna(subset=[col])
                                window_rows["change"] = window_rows["close"] / window_rows[col] - 1
                            else:
                                window_rows["change"] = np.nan
                        triggers = window_rows[window_rows["change"] <= _eff_fall]
                        triggers = triggers.sort_values("timestamp")
                        candidates = triggers.groupby("symbol", as_index=False).head(1)

                        if not candidates.empty and strategy.trigger_require_green_candle:
                            candidates = candidates[candidates["close"] >= candidates["open"]]

                        if not candidates.empty and strategy.trigger_persistence_candles > 0:
                            n = strategy.trigger_persistence_candles
                            thr = strategy.trigger_persistence_threshold
                            kept_indices = []
                            for cand_idx, cand in candidates.iterrows():
                                sym_after = day_prices[
                                    (day_prices["symbol"] == cand["symbol"])
                                    & (day_prices["timestamp"] > cand["timestamp"])
                                ].sort_values("timestamp").head(n)
                                if len(sym_after) < n:
                                    continue
                                prev_close_s = sym_after["prev_close"]
                                if prev_close_s.isna().any():
                                    continue
                                after_change = sym_after["close"] / prev_close_s - 1
                                if (after_change <= thr).all():
                                    kept_indices.append(cand_idx)
                            candidates = candidates.loc[kept_indices]

                if candidates is not None and not candidates.empty:
                    if strategy.rsi_max is not None:
                        candidates = candidates[candidates["rsi14"] < strategy.rsi_max]
                    if strategy.entry_signal == "drop_and_bollinger":
                        candidates = candidates.dropna(subset=["bb_lower20"])
                        candidates = candidates[candidates["close"] <= candidates["bb_lower20"]]
                    if strategy.entry_below_ma20:
                        candidates = candidates.dropna(subset=["bb_mid20"])
                        candidates = candidates[candidates["close"] <= candidates["bb_mid20"]]
                    if strategy.low_proximity_max is not None:
                        candidates = candidates.dropna(subset=["low_90d"])
                        candidates = candidates[
                            (candidates["close"] - candidates["low_90d"]) / candidates["low_90d"]
                            <= strategy.low_proximity_max
                        ]
                    if strategy.entry_below_high_pct is not None:
                        candidates = candidates.dropna(subset=["high_90d"])
                        candidates = candidates[
                            candidates["close"] <= candidates["high_90d"] * (1.0 - strategy.entry_below_high_pct)
                        ]
                    if strategy.sma_above is not None:
                        _sma_col = "sma10" if strategy.sma_above == 10 else ("sma50" if strategy.sma_above == 50 else "bb_mid20")
                        candidates = candidates.dropna(subset=[_sma_col])
                        candidates = candidates[candidates["close"] >= candidates[_sma_col]]
                    # SMA-prev filter — multi-regime path uses _eff_sma_p directly; legacy path
                    # uses strategy.sma_above_prev + sma_above_prev_in_bear gating.
                    _active_sma_p = _eff_sma_p if _use_multiregime else strategy.sma_above_prev
                    if _active_sma_p is not None:
                        _apply_sma_prev = True
                        if not _use_multiregime and strategy.sma_above_prev_in_bear and bear_market_by_date is not None:
                            _nifty_bullish = bool(bear_market_by_date.get(day, True))
                            _apply_sma_prev = not _nifty_bullish  # legacy: only in bear
                        if _apply_sma_prev:
                            _sma_col = "sma10" if _active_sma_p == 10 else ("sma50" if _active_sma_p == 50 else "bb_mid20")
                            candidates = candidates.dropna(subset=[_sma_col, "prev_close"])
                            candidates = candidates[candidates["prev_close"] >= candidates[_sma_col]]
                    # MACD filter — multi-regime path uses _eff_macd directly; legacy path uses
                    # strategy.macd_filter + macd_filter_in_bear_market gating.
                    _active_macd = _eff_macd if _use_multiregime else strategy.macd_filter
                    if _active_macd is not None:
                        _apply_macd = True
                        if not _use_multiregime and strategy.macd_filter_in_bear_market and bear_market_by_date is not None:
                            nifty_bullish = bool(bear_market_by_date.get(day, True))
                            _apply_macd = not nifty_bullish  # legacy: only in bear
                        if _apply_macd:
                            if _active_macd == "positive":
                                candidates = candidates.dropna(subset=["macd_hist"])
                                candidates = candidates[candidates["macd_hist"] > 0]
                            elif _active_macd == "rising":
                                candidates = candidates.dropna(subset=["macd_hist", "macd_hist_3d_ago"])
                                candidates = candidates[candidates["macd_hist"] > candidates["macd_hist_3d_ago"]]
                    if strategy.macd_recent_crossover:
                        candidates = candidates.dropna(subset=["macd_hist", "macd_hist_20d_ago"])
                        candidates = candidates[
                            (candidates["macd_hist"] > 0) & (candidates["macd_hist_20d_ago"] < 0)
                        ]

                    # Sort by largest drop first — biggest intraday capitulation = highest-priority entry.
                    # Already the implicit behavior (sort ascending on change), but made explicit here.
                    candidates = candidates.sort_values("change")
                    if _eff_maxbuys is not None:
                        candidates = candidates.head(_eff_maxbuys)

                    # Pre-compute scan_time closing prices for displacement sell reference
                    scan_closes_for_day = (
                        day_prices[day_prices["time"] <= _scan_t]
                        .sort_values("timestamp")
                        .groupby("symbol")["close"]
                        .last()
                    )

                    # Mark equity for sizing
                    marks_today = day_prices.sort_values("timestamp").groupby("symbol").tail(1).set_index("symbol")["close"]
                    holdings_value = sum(pos["qty"] * marks_today.get(s, pos["avg_price"]) for s, pos in holdings.items())
                    current_equity = cash + holdings_value

                    # Fee-efficiency gate: skip all new entries when free cash is below the minimum.
                    # DP charge on sells (₹15.34) makes small positions very expensive on exit.
                    # This does NOT gate pyramid adds — those are managed by the pyramid logic.
                    if strategy.min_entry_cash is not None and cash < strategy.min_entry_cash:
                        continue  # skip entire scan window for today; no new positions

                    for _, row in candidates.iterrows():
                        symbol = row["symbol"]
                        if symbol in holdings:
                            continue
                        buy_price = float(row["close"]) * (1 + strategy.slippage_rate)
                        if strategy.allocation_mode == "pct_equity":
                            alloc = current_equity * _eff_alloc
                        elif strategy.allocation_mode == "pct_cash":
                            alloc = max(cash * _eff_alloc, 0.0)
                        else:
                            alloc = strategy.allocation_per_trade
                        # Mean-reversion half-life allocation boost.
                        # Faster reverters (shorter halflife) get proportionally more capital.
                        if strategy.mr_halflife_alloc_boost is not None:
                            _hl = row.get("mr_halflife_60d") if hasattr(row, "get") else row["mr_halflife_60d"]
                            if _hl is not None and not (isinstance(_hl, float) and np.isnan(_hl)) and _hl > 0:
                                _fast = strategy.mr_halflife_fast_days
                                _slow = strategy.mr_halflife_slow_days
                                if _hl <= _fast:
                                    _hl_mult = strategy.mr_halflife_alloc_boost
                                elif _hl >= _slow:
                                    _hl_mult = 1.0
                                else:
                                    _t = (_slow - _hl) / max(_slow - _fast, 1e-9)
                                    _hl_mult = 1.0 + (_t * (strategy.mr_halflife_alloc_boost - 1.0))
                                alloc *= _hl_mult
                        if strategy.staged_entry:
                            qty = int((alloc / 2) // buy_price)
                        else:
                            qty = int(alloc // buy_price)
                        if qty <= 0:
                            continue
                        turnover = qty * buy_price
                        fee = delivery_charges_v2(turnover, "BUY", charges)

                        # Position displacement: sell a holding to fund an extreme-drop entry.
                        # Only fires when (a) cash is short, (b) displace_threshold is set,
                        # (c) this candidate's drop exceeds the threshold, and (d) we have holdings.
                        if (cash < turnover + fee
                                and strategy.displace_threshold is not None
                                and float(row["change"]) <= strategy.displace_threshold
                                and holdings):
                            # Choose the holding to sell
                            if strategy.displace_sell_rule == "oldest":
                                sell_sym = min(holdings, key=lambda s: holdings[s]["entry_date"])
                            else:  # "smallest_gain" — sell position with smallest unrealized profit
                                sell_sym = min(
                                    holdings,
                                    key=lambda s: scan_closes_for_day.get(s, holdings[s]["avg_price"]) / holdings[s]["avg_price"],
                                )
                            # Execute the displacement sell at scan-time close
                            disp_holding = holdings[sell_sym]
                            disp_close = float(scan_closes_for_day.get(sell_sym, disp_holding["avg_price"]))
                            disp_sell_price = disp_close * (1 - strategy.slippage_rate)
                            disp_qty = disp_holding["qty"]
                            disp_turnover = disp_qty * disp_sell_price
                            disp_fee = delivery_charges_v2(disp_turnover, "SELL", charges)
                            cash += disp_turnover - disp_fee
                            trades.append({
                                "date": str(day),
                                "time": _scan_t,
                                "symbol": sell_sym, "side": "SELL", "qty": int(disp_qty),
                                "price": round(disp_sell_price, 2), "turnover": round(disp_turnover, 2),
                                "charges": round(disp_fee, 2), "cash_after": round(cash, 2),
                                "reason": f"displace_for_{symbol}_{float(row['change']):+.0%}",
                            })
                            del holdings[sell_sym]
                            # Recompute equity after the displacement sell
                            holdings_value = sum(pos["qty"] * marks_today.get(s, pos["avg_price"]) for s, pos in holdings.items())
                            current_equity = cash + holdings_value

                        if cash < turnover + fee:
                            continue
                        cash -= turnover + fee
                        entry_atr = row.get("atr14") if hasattr(row, "get") else row["atr14"]
                        # Snapshot depth-below-90d-high for adaptive exits (locked at first entry).
                        _high_90d = row.get("high_90d") if hasattr(row, "get") else row["high_90d"]
                        _entry_depth = None
                        if _high_90d is not None and not pd.isna(_high_90d) and _high_90d > 0:
                            _entry_depth = (float(_high_90d) - buy_price) / float(_high_90d) * 100.0
                        holdings[symbol] = _new_holding(
                            qty, buy_price, day, entry_atr,
                            staged_pending=strategy.staged_entry,
                            staged_remaining_alloc=(alloc / 2) if strategy.staged_entry else 0.0,
                            entry_depth_pct=_entry_depth,
                        )
                        trades.append({
                            "date": str(day),
                            "time": row["timestamp"].strftime("%H:%M"),
                            "symbol": symbol, "side": "BUY", "qty": int(qty),
                            "price": round(buy_price, 2), "turnover": round(turnover, 2),
                            "charges": round(fee, 2), "cash_after": round(cash, 2),
                            "reason": f"entry_{strategy.entry_mode}_{_scan_t}_drop_{strategy.fall_threshold:+.0%}",
                        })

        # ---- Mark equity ----
        marks = day_prices.sort_values("timestamp").groupby("symbol").tail(1).set_index("symbol")["close"]
        holdings_value = sum(pos["qty"] * marks.get(symbol, pos["avg_price"]) for symbol, pos in holdings.items())
        equity_curve.append({
            "date": str(day),
            "cash": round(cash, 2),
            "holdings_value": round(float(holdings_value), 2),
            "equity": round(float(cash + holdings_value), 2),
            "open_positions": len(holdings),
        })

    final_equity = equity_curve[-1]["equity"] if equity_curve else strategy.starting_cash
    total_charges = sum(t["charges"] for t in trades)
    buys = sum(1 for t in trades if t["side"] == "BUY")
    sells = sum(1 for t in trades if t["side"] == "SELL")

    # Max drawdown on the equity curve
    equity_series = pd.Series([row["equity"] for row in equity_curve], dtype=float)
    if not equity_series.empty:
        peak = equity_series.cummax()
        dd = (equity_series / peak - 1.0)
        max_dd = float(dd.min()) * 100
    else:
        max_dd = 0.0

    open_positions = [
        {"symbol": s, "qty": int(p["qty"]), "avg_price": round(p["avg_price"], 2), "entry_date": str(p["entry_date"])}
        for s, p in sorted(holdings.items())
    ]
    # Full per-symbol holding state (peak/tiers_hit/entry_atr/entry_depth/...). The slim
    # open_positions list above loses these, and replay.py rebuilds them from the trade
    # list — which fails for ADOPTED positions (they have no BUY trade). Returning the raw
    # holdings dict lets the live trader persist adopted positions to `positions` with full
    # fidelity. Copied so callers can't mutate engine internals.
    holdings_state = {s: dict(p) for s, p in holdings.items()}

    summary = {
        "strategy": strategy.name,
        "starting_cash": round(strategy.starting_cash, 2),
        "final_equity": round(final_equity, 2),
        "profit": round(final_equity - strategy.starting_cash, 2),
        "return_pct": round((final_equity / strategy.starting_cash - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "trades": len(trades),
        "buys": buys,
        "sells": sells,
        "total_charges": round(total_charges, 2),
        "open_positions": len(open_positions),
    }

    return {
        "summary": summary,
        "equity_curve": equity_curve,
        "trades": trades,
        "open_positions": open_positions,
        "holdings_state": holdings_state,  # full per-symbol state dict (incl. adopted positions)
        "deposits": _deposit_log,  # list of {"date", "amount", "cash_after"} — empty when no deposits
        "external_injections": _external_log,  # adopted broker positions — empty when none
        "cash_overrides": _cash_override_log,  # broker cash marks — empty when none
    }
