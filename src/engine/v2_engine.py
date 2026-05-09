"""V2 backtest engine — vendored from the algo project.

Source: ../i-want-to-build-an-algo/engine_v2.py
Sync rule: keep this file byte-for-byte identical to upstream EXCEPT for the
regime-index loading section, which we patched to read from an in-memory cache
(populated by the trader from the DB) instead of CSV files on disk.

If you update upstream engine_v2.py, re-vendor by copying it here and re-applying
the regime patch (clearly marked with `# === paper-trading patch ===` comments).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import pandas as pd


# === paper-trading patch ============================================
# Original code read regime indices from `data/angel_symbols/{name}.csv`.
# Live trader populates this dict from the DB before each replay.
_REGIME_INDEX_CACHE: dict[str, pd.Series] = {}


def prime_regime_index(name: str, daily_close: pd.Series) -> None:
    """Trader calls this once per replay to inject NIFTY_50/SENSEX daily-close
    series. `daily_close` should be indexed by `date` (Python date, not Timestamp).
    """
    _REGIME_INDEX_CACHE[name] = daily_close


def clear_regime_cache() -> None:
    _REGIME_INDEX_CACHE.clear()
# === end patch ======================================================


@dataclass(frozen=True)
class StrategyV2:
    name: str = "default"

    # Entry
    fall_threshold: float = -0.05
    entry_lookback_days: int = 1
    rsi_max: float | None = None
    volume_spike_min: float | None = None
    regime_filter: bool = False

    # Pyramiding
    pyramid_levels: tuple[tuple[float, float], ...] = ()
    pyramid_basis: str = "avg"

    # Exits
    exit_tiers: tuple[tuple[float, float], ...] = ((0.30, 1.0),)

    # Stops
    hard_stop_pct: float | None = None
    time_stop_days: int | None = None
    trail_activate_pct: float | None = None
    trail_drawdown_pct: float | None = None

    # Sizing
    allocation_mode: str = "fixed"
    allocation_per_trade: float = 10000.0
    allocation_pct: float = 0.05

    # Iteration 2
    atr_stop_multiplier: float | None = None
    entry_signal: str = "drop"
    pyramid_volume_filter: bool = False

    # Iteration 4
    entry_mode: str = "scan"
    trigger_window: tuple[str, str] = ("09:30", "15:00")
    low_proximity_max: float | None = None

    # Iteration 5
    regime_source: str | None = None
    trigger_persistence_candles: int = 0
    trigger_persistence_threshold: float = -0.03
    trigger_require_green_candle: bool = False

    # Misc
    scan_time: str = "11:00"
    starting_cash: float = 100000.0
    max_new_buys_per_day: int | None = None
    slippage_rate: float = 0.001


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
    prev_close = group["daily_close"].shift(1)
    tr = pd.concat([
        (group["high"] - group["low"]),
        (group["high"] - prev_close).abs(),
        (group["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def daily_features(prices: pd.DataFrame, scan_time: str) -> pd.DataFrame:
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
    daily_close["low_90d"] = (
        daily_close.groupby("symbol")["low"]
        .transform(lambda s: s.shift(1).rolling(90, min_periods=20).min())
    )

    scan_window = prices[prices["time"] <= scan_time]
    scan_vol = (
        scan_window.groupby(["symbol", "date"], as_index=False)["volume"].sum()
        .rename(columns={"volume": "scan_volume"})
    )
    scan_vol = scan_vol.sort_values(["symbol", "date"]).reset_index(drop=True)
    scan_vol["scan_volume_avg20"] = scan_vol.groupby("symbol")["scan_volume"].transform(
        lambda s: s.shift(1).rolling(20, min_periods=5).mean()
    )
    daily_close = daily_close.merge(
        scan_vol[["symbol", "date", "scan_volume", "scan_volume_avg20"]],
        on=["symbol", "date"], how="left",
    )
    return daily_close


# === paper-trading patch ============================================
def _load_regime_index(name: str) -> pd.Series:
    """Live trader primes _REGIME_INDEX_CACHE from the DB before calling
    run_backtest_v2; tests can do the same. Returns empty series if not primed."""
    return _REGIME_INDEX_CACHE.get(name, pd.Series(dtype=float))
# === end patch ======================================================


def regime_series(daily_features_df: pd.DataFrame, source: str | None = None) -> pd.Series:
    if source is None:
        pivot = daily_features_df.pivot_table(index="date", columns="symbol", values="daily_close")
        rebased = pivot.div(pivot.bfill().iloc[0]).mean(axis=1)
    else:
        index_close = _load_regime_index(source)
        if index_close.empty:
            return regime_series(daily_features_df, source=None)
        feature_dates = daily_features_df["date"].unique()
        rebased = index_close.reindex(sorted(set(index_close.index).union(feature_dates))).ffill()
        rebased = rebased.loc[rebased.index.isin(feature_dates)]
    dma50 = rebased.rolling(50, min_periods=20).mean()
    return rebased > dma50


# ---------- Position bookkeeping ----------

def _new_holding(qty: float, price: float, date, entry_atr: float | None = None) -> dict:
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
    }


def _add_to_holding(holding: dict, add_qty: float, add_price: float) -> None:
    total_qty = holding["qty"] + add_qty
    holding["avg_price"] = (holding["avg_price"] * holding["qty"] + add_price * add_qty) / total_qty
    holding["qty"] = total_qty


# ---------- Engine ----------

def run_backtest_v2(
    prices: pd.DataFrame,
    strategy: StrategyV2,
    charges: ChargeConfigV2,
    start_date=None,
) -> dict:
    """Run V2 backtest. `prices` must have columns: timestamp, symbol, open, high, low, close, volume, date, time.

    Returns dict with summary, equity_curve, trades, open_positions.
    """
    features = daily_features(prices, strategy.scan_time)
    prices = prices.merge(
        features[[
            "symbol", "date",
            "prev_close", "close_2d_ago", "close_3d_ago", "close_5d_ago",
            "rsi14", "atr14",
            "bb_mid20", "bb_lower20", "bb_upper20",
            "low_90d",
            "scan_volume", "scan_volume_avg20",
        ]],
        on=["symbol", "date"],
        how="left",
    )

    regime_ok_by_date = (
        regime_series(features, source=strategy.regime_source)
        if strategy.regime_filter
        else None
    )

    if start_date:
        start = pd.to_datetime(start_date).date()
        prices = prices[prices["date"] >= start]

    cash = float(strategy.starting_cash)
    holdings: dict[str, dict] = {}
    trades: list[dict] = []
    equity_curve: list[dict] = []
    trading_day_idx: dict = {}
    sorted_days = sorted(prices["date"].unique())
    for i, d in enumerate(sorted_days):
        trading_day_idx[d] = i

    for day, day_prices in prices.groupby("date", sort=True):
        day_prices = day_prices.sort_values("timestamp")
        day_idx = trading_day_idx[day]

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

        # ---- Pyramid add pass ----
        for symbol in list(holdings):
            position = holdings[symbol]
            if position["pyramid_adds_hit"] >= len(strategy.pyramid_levels):
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
            drop_pct, alloc_param = strategy.pyramid_levels[level_idx]
            if strategy.pyramid_volume_filter and strategy.volume_spike_min is not None:
                vol = scan_row.get("scan_volume")
                vol_avg = scan_row.get("scan_volume_avg20")
                if vol is None or vol_avg is None or pd.isna(vol_avg) or vol < strategy.volume_spike_min * vol_avg:
                    continue
            trigger = basis * (1 + drop_pct)
            if current_price > trigger:
                continue
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

        # ---- New entry pass ----
        regime_pass = True
        if strategy.regime_filter and regime_ok_by_date is not None:
            regime_pass = bool(regime_ok_by_date.get(day, False))

        if regime_pass:
            candidates = None

            if strategy.entry_mode == "scan":
                scan_rows = (
                    day_prices[day_prices["time"] <= strategy.scan_time]
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

                    candidates = scan_rows[scan_rows["change"] <= strategy.fall_threshold]
                    if strategy.volume_spike_min is not None:
                        candidates = candidates[
                            candidates["scan_volume"]
                            >= strategy.volume_spike_min * candidates["scan_volume_avg20"]
                        ]

            elif strategy.entry_mode == "trigger":
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
                    triggers = window_rows[window_rows["change"] <= strategy.fall_threshold]
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
                            prev_close = sym_after["prev_close"]
                            if prev_close.isna().any():
                                continue
                            after_change = sym_after["close"] / prev_close - 1
                            if (after_change <= thr).all():
                                kept_indices.append(cand_idx)
                        candidates = candidates.loc[kept_indices]

            if candidates is not None and not candidates.empty:
                if strategy.rsi_max is not None:
                    candidates = candidates[candidates["rsi14"] < strategy.rsi_max]
                if strategy.entry_signal == "drop_and_bollinger":
                    candidates = candidates.dropna(subset=["bb_lower20"])
                    candidates = candidates[candidates["close"] <= candidates["bb_lower20"]]
                if strategy.low_proximity_max is not None:
                    candidates = candidates.dropna(subset=["low_90d"])
                    candidates = candidates[
                        (candidates["close"] - candidates["low_90d"]) / candidates["low_90d"]
                        <= strategy.low_proximity_max
                    ]
                candidates = candidates.sort_values("change")
                if strategy.max_new_buys_per_day is not None:
                    candidates = candidates.head(strategy.max_new_buys_per_day)

                marks_today = day_prices.sort_values("timestamp").groupby("symbol").tail(1).set_index("symbol")["close"]
                holdings_value = sum(pos["qty"] * marks_today.get(s, pos["avg_price"]) for s, pos in holdings.items())
                current_equity = cash + holdings_value

                for _, row in candidates.iterrows():
                    symbol = row["symbol"]
                    if symbol in holdings:
                        continue
                    buy_price = float(row["close"]) * (1 + strategy.slippage_rate)
                    if strategy.allocation_mode == "pct_equity":
                        alloc = current_equity * strategy.allocation_pct
                    elif strategy.allocation_mode == "pct_cash":
                        alloc = max(cash * strategy.allocation_pct, 0.0)
                    else:
                        alloc = strategy.allocation_per_trade
                    qty = int(alloc // buy_price)
                    if qty <= 0:
                        continue
                    turnover = qty * buy_price
                    fee = delivery_charges_v2(turnover, "BUY", charges)
                    if cash < turnover + fee:
                        continue
                    cash -= turnover + fee
                    entry_atr = row.get("atr14") if hasattr(row, "get") else row["atr14"]
                    holdings[symbol] = _new_holding(qty, buy_price, day, entry_atr)
                    trades.append({
                        "date": str(day),
                        "time": row["timestamp"].strftime("%H:%M"),
                        "symbol": symbol, "side": "BUY", "qty": int(qty),
                        "price": round(buy_price, 2), "turnover": round(turnover, 2),
                        "charges": round(fee, 2), "cash_after": round(cash, 2),
                        "reason": f"entry_{strategy.entry_mode}_drop_{strategy.fall_threshold:+.0%}",
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
    }
