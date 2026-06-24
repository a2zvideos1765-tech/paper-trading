"""Backfill queue worker.

When a user adds a new symbol via /symbols with "Backfill 200 days" checked,
the API enqueues a row in `backfill_queue` and returns immediately. This worker
runs overnight (via PM2 cron at 18:00 IST) and drains the queue one symbol at
a time, paced at ~1 fetch/sec to stay well below Angel's rate limit.

If the queue isn't empty by 06:00 IST (next trading day approaches), we exit
cleanly — remaining rows roll to tomorrow night.

PM2 schedule: `0 18 * * *`  (every day 18:00 IST, autorestart=false).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime, timedelta

from src.core.angel import AngelClient
from src.core.db import close_pool, conn, fetch, get_pool, heartbeat
from src.core.logging import setup_logging
from src.core.time import IST, now_ist


log = setup_logging("backfill_queue")

PACE_SECONDS = 1.25                # mirrors load_history pacing; well below Angel's limits
DEADLINE_HOUR_IST = 6              # stop at 06:00 IST (before market warm-up)


async def _claim_one() -> dict | None:
    """Atomically mark the oldest pending row as running, return it. Returns None if empty."""
    async with conn() as c:
        row = await c.fetchrow(
            """
            UPDATE backfill_queue
               SET state = 'running', started_at = now()
             WHERE id = (
                 SELECT id FROM backfill_queue
                  WHERE state = 'pending'
                  ORDER BY enqueued_at
                  LIMIT 1
                  FOR UPDATE SKIP LOCKED
             )
            RETURNING id, symbol, exchange, token, interval, days
            """,
        )
    return dict(row) if row else None


async def _mark_done(qid: int, rows_inserted: int) -> None:
    async with conn() as c:
        await c.execute(
            """
            UPDATE backfill_queue
               SET state = 'done', finished_at = now(),
                   error = NULL
             WHERE id = $1
            """,
            qid,
        )
    log.info("backfill done", extra={"queue_id": qid, "rows": rows_inserted})


async def _mark_error(qid: int, exc: Exception) -> None:
    async with conn() as c:
        await c.execute(
            """
            UPDATE backfill_queue
               SET state = 'error', finished_at = now(),
                   error = $2
             WHERE id = $1
            """,
            qid, str(exc)[:500],
        )
    log.error("backfill failed", extra={"queue_id": qid, "err": str(exc)[:200]})


async def _upsert_bars(symbol: str, interval: str, df) -> int:
    if df.empty:
        return 0
    rows = []
    for ts, _, o, h, l, c, v in df[["timestamp", "symbol", "open", "high", "low", "close", "volume"]].itertuples(index=False):
        if ts.tzinfo is None:
            ts = ts.tz_localize(IST)
        else:
            ts = ts.tz_convert(IST)
        rows.append((symbol, interval, ts.to_pydatetime(),
                     float(o), float(h), float(l), float(c), int(v)))
    async with conn() as cn:
        # Same chunking pattern as tools/load_history.py to avoid timeouts on big windows.
        CHUNK = 1000
        for i in range(0, len(rows), CHUNK):
            await cn.executemany(
                """
                INSERT INTO candles (symbol, interval, ts, open, high, low, close, volume)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (symbol, interval, ts) DO NOTHING
                """,
                rows[i : i + CHUNK],
            )
    return len(rows)


async def _run_one(client: AngelClient, item: dict) -> int:
    today = now_ist().date()
    from_dt = datetime.combine(today - timedelta(days=int(item["days"])), dtime(9, 15))
    to_dt = datetime.combine(today, dtime(15, 30))

    df = client.get_candle(
        symbol=item["symbol"],
        token=item["token"],
        exchange=item["exchange"],
        interval=item["interval"],
        from_dt=from_dt,
        to_dt=to_dt,
    )
    n = await _upsert_bars(item["symbol"], item["interval"], df)
    log.info("backfill chunk", extra={
        "symbol": item["symbol"], "interval": item["interval"],
        "rows": n, "from": from_dt.isoformat(), "to": to_dt.isoformat(),
    })
    return n


def _past_deadline(start: datetime) -> bool:
    now = now_ist()
    # Worker starts at ~18:00 IST and runs overnight until 06:00 IST next morning.
    # Compute 06:00 IST on the start day; if that's already in the past (it always
    # is when we launch at 18:00), roll to 06:00 IST tomorrow.
    deadline = start.replace(hour=DEADLINE_HOUR_IST, minute=0, second=0, microsecond=0)
    if deadline <= start:
        deadline += timedelta(days=1)
    return now >= deadline


async def main() -> None:
    await get_pool()
    log.info("backfill queue worker starting")
    await heartbeat("backfill_queue", "ok", detail="starting")

    started = now_ist()
    processed = 0

    try:
        # Pending count for visibility
        pending = await fetch("SELECT count(*) AS n FROM backfill_queue WHERE state = 'pending'")
        log.info("queue status", extra={"pending": int(pending[0]["n"])})

        if int(pending[0]["n"]) == 0:
            await heartbeat("backfill_queue", "ok", detail="queue empty")
            return

        client = AngelClient.for_data()
        log.info("angel login ok", extra={"account": client.account})

        while True:
            if _past_deadline(started):
                log.info("deadline reached, stopping")
                break

            item = await _claim_one()
            if item is None:
                log.info("queue drained")
                break

            try:
                rows = await _run_one(client, item)
                await _mark_done(item["id"], rows)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("backfill item failed", extra={"queue_id": item["id"]})
                await _mark_error(item["id"], exc)

            await asyncio.sleep(PACE_SECONDS)

        await heartbeat("backfill_queue", "ok",
                        detail=f"processed {processed} item(s)")
        log.info("backfill queue worker done", extra={"processed": processed})
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
