"""Paper-trading runner.

Once per minute (offset slightly after the poller writes), for each enabled
portfolio in config/portfolios.yaml:
  1. Load a rolling-window of candles + index data from the DB
  2. Run engine_v2.run_backtest_v2 against it (the strategy logic — same code
     path the backtester uses)
  3. Diff the engine's trades against `trades` table; insert new ones
  4. Replace `positions` snapshot; upsert daily `equity_snapshots`
  5. Heartbeat → `runs.last_beat`

Adding a new portfolio: edit config/portfolios.yaml, restart this process.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path

import yaml

from src.core.config import REPO_ROOT, settings
from src.core.db import close_pool, conn, get_pool, heartbeat
from src.core.logging import setup_logging
from src.core.time import is_market_open, now_ist, seconds_until_market_open
from src.core.universe import all_specs
from src.engine.replay import (
    DEFAULT_LOOKBACK_DAYS,
    PortfolioRow,
    load_candles_window,
    load_index_close,
    load_portfolios,
    replay_one_portfolio,
)
from src.engine.v2_engine import ChargeConfigV2
from src.strategies.registry import all_strategies, get as get_strategy


log = setup_logging("trader")
CHARGES = ChargeConfigV2()
CANDLE_INTERVAL = "5m"  # Engine was tuned on 5m bars; matches existing CSV history.


# ---------- Bootstrap ----------

async def sync_portfolios_from_yaml() -> None:
    """Read config/portfolios.yaml; UPSERT each row into `portfolios`. Anything
    enabled in YAML becomes enabled=true in DB; anything previously in DB but
    missing from YAML stays in DB but flips to enabled=false (so its history
    survives but no new trades happen)."""
    path = REPO_ROOT / "config" / "portfolios.yaml"
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    yaml_rows = data.get("portfolios") or []
    yaml_names = {r["name"] for r in yaml_rows}

    # Validate every referenced strategy actually exists in the registry.
    known = set(all_strategies().keys())
    for r in yaml_rows:
        if r["strategy"] not in known:
            raise SystemExit(
                f"portfolios.yaml references strategy {r['strategy']!r} but no "
                f"src/strategies/*.py exports it. Known: {sorted(known)}"
            )

    async with conn() as c:
        for r in yaml_rows:
            await c.execute(
                """
                INSERT INTO portfolios (name, strategy_id, capital, enabled)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (name) DO UPDATE
                  SET strategy_id = EXCLUDED.strategy_id,
                      capital = EXCLUDED.capital,
                      enabled = EXCLUDED.enabled
                """,
                r["name"], r["strategy"], float(r["capital"]),
                bool(r.get("enabled", True)),
            )
        # Mark any existing DB row not in YAML as disabled.
        await c.execute(
            "UPDATE portfolios SET enabled = FALSE WHERE name <> ALL($1::text[])",
            list(yaml_names),
        )

    log.info("portfolios synced", extra={"yaml_count": len(yaml_rows)})


# ---------- One tick ----------

async def tick() -> None:
    portfolios: list[PortfolioRow] = await load_portfolios()
    if not portfolios:
        log.warning("no enabled portfolios")
        return

    # Load all candles once, share across portfolios. We use 5-min bars for the
    # equity universe (matches the engine's training data) and 1-day for indices.
    symbols = [s.symbol for s in all_specs() if s.exchange == "NSE"]
    # Filter to equities — drop indices from the equity candle window.
    from src.core.universe import load_universe
    equities, indices = load_universe()
    equity_symbols = [s.symbol for s in equities]

    until = now_ist().replace(second=0, microsecond=0)
    since = until - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    candles = await load_candles_window(equity_symbols, CANDLE_INTERVAL, since, until)

    if candles.empty:
        log.warning("no candles in lookback window — skipping tick",
                    extra={"since": since.isoformat(), "until": until.isoformat()})
        await heartbeat("trader", "sleeping", detail="no candles loaded")
        return

    nifty   = await load_index_close("NIFTY_50", interval="1d")
    sensex  = await load_index_close("SENSEX",   interval="1d")

    for p in portfolios:
        try:
            strategy = get_strategy(p.strategy_id)
            await replay_one_portfolio(p, strategy, candles, CHARGES, nifty, sensex)
        except Exception as exc:  # noqa: BLE001
            log.exception("portfolio replay failed",
                          extra={"portfolio_id": p.id, "portfolio_name": p.name})

    await heartbeat("trader", "ok", detail=f"replayed {len(portfolios)} portfolios")


# ---------- Loop ----------

async def main() -> None:
    await get_pool()
    log.info("trader starting", extra={"tick_seconds": settings.trader_interval_seconds,
                                        "offset_seconds": settings.trader_offset_seconds,
                                        "lookback_days": DEFAULT_LOOKBACK_DAYS})
    await sync_portfolios_from_yaml()

    # Initial start-up offset so the first tick fires after the poller has had a chance to write.
    await asyncio.sleep(settings.trader_offset_seconds)

    try:
        while True:
            if not is_market_open():
                wait = max(60.0, min(seconds_until_market_open(), 1800.0))
                await heartbeat("trader", "sleeping", detail="market closed")
                log.info("market closed, sleeping", extra={"wait_seconds": wait})
                await asyncio.sleep(wait)
                continue

            cycle_start = time.monotonic()
            try:
                await tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("tick errored")
                await heartbeat("trader", "error", detail=str(exc)[:200])

            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.0, settings.trader_interval_seconds - elapsed)
            await asyncio.sleep(sleep_for)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
