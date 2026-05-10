"""Replay-driven live trader.

Key idea: the live trader runs the same `run_backtest_v2` engine that the backtester
does, on a rolling window of historical candles loaded from the DB. We do this every
minute. The engine outputs the *full* trade list since the strategy started; we diff
it against what's already in the `trades` table and INSERT the new ones (idempotent
via UNIQUE INDEX).

This guarantees parity by construction — there's no separate "live tick" code path
to drift from the backtester.

Performance: at our scale (~50 symbols × 200 trading days × ~75 5-min bars/day ≈
~750k rows) the replay takes <2s per portfolio. Comfortable for a 60-second tick.
If this becomes a bottleneck later we can cache `daily_features` between minutes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

import asyncpg
import pandas as pd

from src.core.db import conn, fetchrow
from src.core.time import IST
from src.engine.v2_engine import (
    ChargeConfigV2,
    StrategyV2,
    clear_regime_cache,
    prime_regime_index,
    run_backtest_v2,
)
from src.strategies.validation import coerce_and_apply


log = logging.getLogger("engine.replay")


# Default lookback for the rolling replay window. Long enough for RSI/ATR/BB warmup
# and the 90-day low feature; small enough to keep each replay snappy.
DEFAULT_LOOKBACK_DAYS = 200


@dataclass(frozen=True)
class PortfolioRow:
    id: int
    name: str
    strategy_id: str
    capital: float
    enabled: bool


# ---------- Loading state from DB ----------

async def load_portfolios() -> list[PortfolioRow]:
    async with conn() as c:
        rows = await c.fetch(
            "SELECT id, name, strategy_id, capital::float8, enabled "
            "FROM portfolios WHERE enabled = TRUE ORDER BY id"
        )
    return [PortfolioRow(**dict(r)) for r in rows]


async def load_candles_window(
    symbols: list[str],
    interval: str,
    since: datetime,
    until: datetime,
) -> pd.DataFrame:
    """Pull a rolling window of candles into a DataFrame shaped like the backtester
    expects: timestamp, symbol, open, high, low, close, volume, date, time."""
    if not symbols:
        return pd.DataFrame(columns=[
            "timestamp", "symbol", "open", "high", "low", "close", "volume", "date", "time"
        ])
    async with conn() as c:
        rows = await c.fetch(
            """
            SELECT symbol, ts AS timestamp,
                   open::float8 AS open, high::float8 AS high,
                   low::float8 AS low, close::float8 AS close, volume
            FROM candles
            WHERE interval = $1
              AND symbol = ANY($2::text[])
              AND ts >= $3 AND ts <= $4
            ORDER BY ts
            """,
            interval, list(symbols), since, until,
        )
    if not rows:
        return pd.DataFrame(columns=[
            "timestamp", "symbol", "open", "high", "low", "close", "volume", "date", "time"
        ])
    df = pd.DataFrame([dict(r) for r in rows])
    # Engine wants IST-localized timestamps; the DB returns timezone-aware UTC.
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert(IST)
    df["date"] = df["timestamp"].dt.date
    df["time"] = df["timestamp"].dt.strftime("%H:%M")
    return df.sort_values(["timestamp", "symbol"]).reset_index(drop=True)


async def load_index_close(symbol: str, interval: str = "1d") -> pd.Series:
    """Load NIFTY_50 / SENSEX daily close as a Series indexed by Python date.
    Used to prime engine_v2's regime cache before each replay."""
    async with conn() as c:
        rows = await c.fetch(
            """
            SELECT ts, close::float8 AS close
            FROM candles
            WHERE symbol = $1 AND interval = $2
            ORDER BY ts
            """,
            symbol, interval,
        )
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame([dict(r) for r in rows])
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_convert(IST)
    df["date"] = df["ts"].dt.date
    return df.groupby("date")["close"].last()


# ---------- Persisting engine output ----------

async def upsert_trades(portfolio_id: int, trades: list[dict]) -> int:
    """Insert any trades the engine produced that aren't already in the table.
    Returns the number of new rows actually inserted."""
    if not trades:
        return 0
    rows = []
    for t in trades:
        # Engine's `date` is a string YYYY-MM-DD, `time` is HH:MM (IST). Combine into a TIMESTAMPTZ.
        dt_naive = datetime.strptime(f"{t['date']} {t['time']}", "%Y-%m-%d %H:%M")
        ts = dt_naive.replace(tzinfo=IST)
        rows.append((
            portfolio_id,
            t["symbol"],
            t["side"],
            int(t["qty"]),
            float(t["price"]),
            float(t["turnover"]),
            float(t["charges"]),
            float(t["cash_after"]),
            ts,
            t["reason"],
        ))
    async with conn() as c:
        result = await c.executemany(
            """
            INSERT INTO trades
                (portfolio_id, symbol, side, qty, price, turnover, charges, cash_after, ts, reason)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (portfolio_id, ts, symbol, side, qty, price, reason) DO NOTHING
            """,
            rows,
        )
    # asyncpg's executemany doesn't return per-statement counts; query the count we want.
    async with conn() as c:
        new_count = await c.fetchval(
            "SELECT COUNT(*) FROM trades WHERE portfolio_id = $1",
            portfolio_id,
        )
    return int(new_count)


async def replace_positions(portfolio_id: int, open_positions_state: dict[str, dict]) -> None:
    """Replace the positions table snapshot for this portfolio with the engine's
    holdings dict. Done in a single transaction for atomicity."""
    async with conn() as c:
        async with c.transaction():
            await c.execute("DELETE FROM positions WHERE portfolio_id = $1", portfolio_id)
            if not open_positions_state:
                return
            rows = []
            for symbol, h in open_positions_state.items():
                rows.append((
                    portfolio_id, symbol,
                    int(h["qty"]),
                    float(h["avg_price"]),
                    float(h["entry_price"]),
                    h["entry_date"] if isinstance(h["entry_date"], date) else date.fromisoformat(str(h["entry_date"])),
                    float(h["peak_price"]),
                    int(h["pyramid_adds_hit"]),
                    int(h["tiers_hit"]),
                    bool(h["trail_armed"]),
                    float(h["entry_atr"]) if h.get("entry_atr") is not None else None,
                ))
            await c.executemany(
                """
                INSERT INTO positions
                    (portfolio_id, symbol, qty, avg_price, entry_price, entry_date,
                     peak_price, pyramid_adds_hit, tiers_hit, trail_armed, entry_atr)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                rows,
            )


async def upsert_equity_curve(portfolio_id: int, curve: list[dict]) -> None:
    if not curve:
        return
    rows = [
        (
            portfolio_id,
            datetime.combine(date.fromisoformat(row["date"]), datetime.min.time(), tzinfo=IST),
            float(row["cash"]),
            float(row["holdings_value"]),
            float(row["equity"]),
            int(row["open_positions"]),
        )
        for row in curve
    ]
    async with conn() as c:
        await c.executemany(
            """
            INSERT INTO equity_snapshots
                (portfolio_id, ts, cash, holdings_value, equity, open_positions)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (portfolio_id, ts) DO UPDATE
                SET cash = EXCLUDED.cash,
                    holdings_value = EXCLUDED.holdings_value,
                    equity = EXCLUDED.equity,
                    open_positions = EXCLUDED.open_positions
            """,
            rows,
        )


# ---------- Engine internals: extract holdings dict from a finished replay ----------

def _holdings_from_open_positions(open_positions: list[dict], all_trades: list[dict]) -> dict[str, dict]:
    """The engine returns `open_positions` as a flat list (symbol, qty, avg, entry_date)
    but the dashboard wants the full holdings dict (peak, tiers_hit, ...). Reconstruct it
    by replaying the trade list — same algorithm as engine_v2._new_holding/_add_to_holding."""
    state: dict[str, dict] = {}
    # Bucket trades by symbol in chronological order.
    trades_by_symbol: dict[str, list[dict]] = {}
    for t in all_trades:
        trades_by_symbol.setdefault(t["symbol"], []).append(t)

    for op in open_positions:
        sym = op["symbol"]
        ts = trades_by_symbol.get(sym, [])
        # Find the chain of BUYs since the last full-close; then count tier exits / pyramid adds.
        buys: list[dict] = []
        sells: list[dict] = []
        for t in ts:
            if t["side"] == "BUY":
                buys.append(t)
            else:
                sells.append(t)
        if not buys:
            continue
        # Walk the trade history; whenever a SELL closes the position fully, reset.
        running_qty = 0
        chain_buys: list[dict] = []
        chain_sells: list[dict] = []
        for t in ts:
            if t["side"] == "BUY":
                running_qty += t["qty"]
                chain_buys.append(t)
            else:
                running_qty -= t["qty"]
                chain_sells.append(t)
                if running_qty <= 0:
                    chain_buys.clear()
                    chain_sells.clear()
                    running_qty = 0
        if not chain_buys:
            continue
        first_buy = chain_buys[0]
        # tiers_hit = number of target_*_tier* sells in this chain
        tiers_hit = sum(1 for s in chain_sells if s["reason"].startswith("target_"))
        # pyramid_adds_hit = number of pyramid_* buys in this chain
        pyramid_adds_hit = sum(1 for b in chain_buys if b["reason"].startswith("pyramid_"))
        # weighted avg
        total_qty = sum(b["qty"] for b in chain_buys) - sum(s["qty"] for s in chain_sells)
        if total_qty <= 0:
            continue
        cost_basis = sum(b["qty"] * b["price"] for b in chain_buys)
        # Approximate by assuming each sell sold proportionally — this matches engine_v2's
        # avg_price math because partial sells don't change avg_price there, only qty.
        # (The engine recomputes avg only on adds via _add_to_holding.)
        avg_buys_qty = sum(b["qty"] for b in chain_buys)
        avg_price = cost_basis / avg_buys_qty if avg_buys_qty else first_buy["price"]
        state[sym] = {
            "qty": float(total_qty),
            "avg_price": avg_price,
            "entry_price": first_buy["price"],
            "entry_date": date.fromisoformat(first_buy["date"]),
            "peak_price": max(b["price"] for b in chain_buys),  # lower bound; refined by engine on next replay
            "pyramid_adds_hit": pyramid_adds_hit,
            "tiers_hit": tiers_hit,
            "trail_armed": False,  # engine recomputes on next tick
            "entry_atr": None,
        }
    return state


# ---------- Top-level: one replay for one portfolio ----------

async def _load_overrides(portfolio_id: int) -> dict:
    row = await fetchrow(
        "SELECT overrides FROM portfolio_overrides WHERE portfolio_id = $1",
        portfolio_id,
    )
    if not row:
        return {}
    val = row["overrides"]
    return val if isinstance(val, dict) else {}


async def replay_one_portfolio(
    portfolio: PortfolioRow,
    strategy: StrategyV2,
    candles: pd.DataFrame,
    charges: ChargeConfigV2,
    nifty_close: pd.Series,
    sensex_close: pd.Series,
) -> dict:
    """Run the engine for this portfolio against the given candles window.
    Returns the engine's full result dict and persists trades / positions / equity."""
    clear_regime_cache()
    if not nifty_close.empty:
        prime_regime_index("NIFTY_50", nifty_close)
    if not sensex_close.empty:
        prime_regime_index("SENSEX", sensex_close)

    # Apply per-portfolio strategy overrides (set by the user from the dashboard).
    # If validation fails, skip this portfolio's tick — never silently corrupt state.
    overrides = await _load_overrides(portfolio.id)
    overridden, errs = coerce_and_apply(strategy, overrides)
    if errs:
        log.warning(
            "skipping portfolio: invalid overrides",
            extra={"portfolio_id": portfolio.id, "portfolio_name": portfolio.name, "errors": errs},
        )
        return {"trades": [], "open_positions": [], "equity_curve": [],
                "summary": {"final_equity": float(portfolio.capital)},
                "validation_errors": errs}

    # Bind the portfolio's capital to the (possibly overridden) strategy.
    bound = replace(overridden, starting_cash=float(portfolio.capital))

    result = run_backtest_v2(candles, bound, charges)

    # Persist
    await upsert_trades(portfolio.id, result["trades"])
    holdings_state = _holdings_from_open_positions(result["open_positions"], result["trades"])
    await replace_positions(portfolio.id, holdings_state)
    await upsert_equity_curve(portfolio.id, result["equity_curve"])

    log.info(
        "replay completed",
        extra={
            "portfolio_id": portfolio.id,
            "portfolio_name": portfolio.name,
            "trades": len(result["trades"]),
            "open_positions": len(result["open_positions"]),
            "final_equity": result["summary"]["final_equity"],
        },
    )
    return result
