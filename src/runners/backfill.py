"""Nightly backfill — runs once per weekday after market close (PM2 cron_restart).

For each symbol in universe.yaml + each index, find the most recent bar in the
candles table for the relevant interval and pull anything missing from Angel up
to today's close. Also fetches daily bars for NIFTY_50 / SENSEX so the regime
filter has fresh data.

This keeps the live trader off the Angel REST API for everything except the
trailing-minute poll during market hours.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime, timedelta

from src.core.angel import AngelClient
from src.core.db import close_pool, conn, get_pool, heartbeat
from src.core.logging import setup_logging
from src.core.time import IST, now_ist
from src.core.universe import load_universe


log = setup_logging("backfill")


# Intervals to backfill per symbol category. Kept conservative: 5m for equities
# matches the algo project's history; 1d for indices is enough for regime filter.
EQUITY_INTERVALS = ["5m", "1m"]
INDEX_INTERVALS  = ["1d"]


async def latest_ts(symbol: str, interval: str) -> datetime | None:
    async with conn() as c:
        row = await c.fetchval(
            "SELECT max(ts) FROM candles WHERE symbol = $1 AND interval = $2",
            symbol, interval,
        )
    return row


async def upsert_bars(symbol: str, interval: str, df) -> int:
    if df.empty:
        return 0
    rows = []
    for ts, _, o, h, l, c, v in df[["timestamp", "symbol", "open", "high", "low", "close", "volume"]].itertuples(index=False):
        if ts.tzinfo is None:
            ts = ts.tz_localize(IST)
        else:
            ts = ts.tz_convert(IST)
        rows.append((symbol, interval, ts.to_pydatetime(), float(o), float(h), float(l), float(c), int(v)))
    async with conn() as cn:
        await cn.executemany(
            """
            INSERT INTO candles (symbol, interval, ts, open, high, low, close, volume)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (symbol, interval, ts) DO NOTHING
            """,
            rows,
        )
    return len(rows)


async def backfill_one(client: AngelClient, spec, interval: str) -> int:
    """Pull from `latest_ts(symbol, interval) + 1` up to the last completed market session."""
    last = await latest_ts(spec.symbol, interval)
    today = now_ist().date()

    if interval == "1d":
        from_dt = (last.astimezone(IST) + timedelta(days=1)).replace(tzinfo=None) if last else datetime.combine(today - timedelta(days=400), dtime(9, 15))
        to_dt = datetime.combine(today, dtime(15, 30))
    else:
        from_dt = (last.astimezone(IST) + timedelta(minutes=1)).replace(tzinfo=None) if last else datetime.combine(today - timedelta(days=10), dtime(9, 15))
        to_dt = datetime.combine(today, dtime(15, 30))

    if from_dt >= to_dt:
        return 0

    df = client.get_candle(
        symbol=spec.symbol,
        token=spec.token,
        exchange=spec.exchange,
        interval=interval,
        from_dt=from_dt,
        to_dt=to_dt,
    )
    n = await upsert_bars(spec.symbol, interval, df)
    log.info("backfilled", extra={"symbol": spec.symbol, "interval": interval, "rows": n,
                                   "from": from_dt.isoformat(), "to": to_dt.isoformat()})
    await asyncio.sleep(1.25)
    return n


async def main() -> None:
    await get_pool()
    log.info("backfill starting")
    await heartbeat("backfill", "ok", detail="starting")

    try:
        client = AngelClient.for_data()
        log.info("angel login ok", extra={"account": client.account})

        equities, indices = await load_universe()
        total = 0

        for spec in equities:
            for interval in EQUITY_INTERVALS:
                try:
                    total += await backfill_one(client, spec, interval)
                except Exception as exc:  # noqa: BLE001
                    log.exception("backfill failed", extra={"symbol": spec.symbol, "interval": interval})

        for spec in indices:
            for interval in INDEX_INTERVALS:
                try:
                    total += await backfill_one(client, spec, interval)
                except Exception as exc:  # noqa: BLE001
                    log.exception("backfill failed", extra={"symbol": spec.symbol, "interval": interval})

        await heartbeat("backfill", "ok", detail=f"backfilled {total} rows")
        log.info("backfill done", extra={"total_rows": total})
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
