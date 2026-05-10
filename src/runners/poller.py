"""Live data poller: once per minute during market hours, fetch the most recent
5-minute candles for each symbol in config/universe.yaml and upsert to `candles`.

Why 5-minute, not 1-minute? The engine (and the entire backtest grid) was tuned
on 5-min bars; the historical CSVs in `data/angel_symbols/` are 5-min bars. The
trader queries `interval='5m'` for replay (see runners/trader.py CANDLE_INTERVAL).
Writing 1-min bars from the poller would leave the trader blind during market
hours — it would only see the previous night's backfill. Keep one interval
end-to-end.

The nightly backfill still pulls 1-min bars too (see runners/backfill.py
EQUITY_INTERVALS) so the dashboard can offer a finer-grained chart later if
needed; live polling sticks to 5m.

We poll every minute even though the bar is 5 minutes long: each poll fetches a
trailing 5-minute window, so the still-forming current bar gets refreshed via
ON CONFLICT DO UPDATE until it finalises. This keeps trader latency low without
extra Angel calls per symbol.

Run via PM2:  pm2 start ecosystem.config.js --only paperaglo-poller
Run manually: python -m src.runners.poller
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

from src.core.angel import AngelClient
from src.core.config import settings
from src.core.db import close_pool, conn, get_pool, heartbeat
from src.core.logging import setup_logging
from src.core.time import IST, is_market_open, now_ist, seconds_until_market_open
from src.core.universe import all_specs


log = setup_logging("poller")
INTERVAL = "5m"  # MUST match runners/trader.py CANDLE_INTERVAL and load_history.py default.


async def upsert_bars(symbol: str, df) -> int:
    if df.empty:
        return 0
    rows = []
    for ts, _, o, h, l, c, v in df[["timestamp", "symbol", "open", "high", "low", "close", "volume"]].itertuples(index=False):
        ts = ts.tz_convert(IST) if hasattr(ts, "tz_convert") and ts.tzinfo else ts.tz_localize(IST) if ts.tzinfo is None else ts
        rows.append((symbol, INTERVAL, ts.to_pydatetime(), float(o), float(h), float(l), float(c), int(v)))
    async with conn() as c:
        await c.executemany(
            """
            INSERT INTO candles (symbol, interval, ts, open, high, low, close, volume)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (symbol, interval, ts) DO UPDATE
                SET open = EXCLUDED.open, high = EXCLUDED.high,
                    low = EXCLUDED.low, close = EXCLUDED.close, volume = EXCLUDED.volume
            """,
            rows,
        )
    return len(rows)


async def poll_once(client: AngelClient) -> None:
    """One pass over the universe — fetch the trailing few 5-min bars and upsert."""
    specs = all_specs()
    now = now_ist()
    # 15-minute trailing window: covers the in-progress 5-min bar plus the two
    # prior bars, so a single missed cycle self-heals on the next poll. UPSERTs
    # are cheap; over-fetching by a couple of bars is the right trade-off.
    since = (now - timedelta(minutes=15)).replace(second=0, microsecond=0)
    until = now.replace(second=0, microsecond=0)

    total = 0
    for spec in specs:
        try:
            df = client.get_candle(
                symbol=spec.symbol,
                token=spec.token,
                exchange=spec.exchange,
                interval=INTERVAL,
                from_dt=since.replace(tzinfo=None),
                to_dt=until.replace(tzinfo=None),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("poll failed", extra={"symbol": spec.symbol, "err": str(exc)})
            continue
        n = await upsert_bars(spec.symbol, df)
        total += n
        # 1.25s pause between symbols mirrors algo project's rate-limit headroom.
        await asyncio.sleep(1.25)

    await heartbeat("poller", "ok", detail=f"upserted {total} candle rows")
    log.info("poll cycle done", extra={"upserted": total, "symbols": len(specs)})


async def main() -> None:
    await get_pool()
    log.info("poller starting", extra={"interval": INTERVAL,
                                       "tick_seconds": settings.poller_interval_seconds})

    # On boot, log in once. If credentials fail we exit loudly so PM2 restarts.
    client = AngelClient.login()
    log.info("angel login ok")

    try:
        while True:
            if not is_market_open():
                wait = max(60.0, min(seconds_until_market_open(), 1800.0))
                await heartbeat("poller", "sleeping", detail="market closed")
                log.info("market closed, sleeping", extra={"wait_seconds": wait})
                await asyncio.sleep(wait)
                continue

            cycle_start = time.monotonic()
            try:
                await poll_once(client)
            except Exception as exc:  # noqa: BLE001
                log.exception("poll cycle errored")
                await heartbeat("poller", "error", detail=str(exc)[:200])

            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.0, settings.poller_interval_seconds - elapsed)
            await asyncio.sleep(sleep_for)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
