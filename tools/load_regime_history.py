"""Seed NIFTY 50 + India VIX daily history into the `candles` table.

The multi-regime strategies (S228, S283) classify every trading day as
bull/bear/sideways from NIFTY 50's DMA structure plus an India-VIX fear
override. The classifier needs ~50 daily bars of each to compute a 50-DMA on
day one — it can't wait for the nightly backfill to accumulate that.

This tool seeds ~8 years of daily closes from the bundled CSVs so the
classifier works the moment the trader restarts. From then on the nightly
`paperaglo-backfill` job keeps both current (it backfills every `kind='index'`
symbol in the universe).

Both bars are stored at 00:00 IST — matching Angel's ONE_DAY candle stamp — so
ON CONFLICT DO NOTHING dedupes cleanly against anything the backfill already
wrote. The CSVs carry only a close price; the regime code reads only `close`,
so open=high=low=close and volume=0.

Run once, after sql/005_india_vix.sql:
    python -m tools.load_regime_history

Idempotent — safe to re-run.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pandas as pd

from src.core.config import REPO_ROOT
from src.core.db import close_pool, conn, get_pool
from src.core.logging import setup_logging
from src.core.time import IST


log = setup_logging("load_regime")

INTERVAL = "1d"
BATCH = 1000

# Each source: (db_symbol, csv_filename, read_fn) — the two CSVs have different
# header layouts so each gets its own tiny reader.
DATA_DIR = REPO_ROOT / "data"


def _read_nifty() -> list[tuple[datetime, float]]:
    """NIFTY_50_extended.csv — clean `date,close` header."""
    path = DATA_DIR / "NIFTY_50_extended.csv"
    if not path.exists():
        raise SystemExit(f"missing {path} — confirm `git pull` brought it to the VPS")
    df = pd.read_csv(path, parse_dates=["date"])
    return _rows(df.set_index("date")["close"])


def _read_vix() -> list[tuple[datetime, float]]:
    """INDIA_VIX_extended.csv — three header lines (Price/Ticker/Date) then date,close."""
    path = DATA_DIR / "INDIA_VIX_extended.csv"
    if not path.exists():
        raise SystemExit(f"missing {path} — confirm `git pull` brought it to the VPS")
    df = pd.read_csv(path, skiprows=[1, 2], index_col=0, parse_dates=True)
    df = df[pd.notnull(df.index)]
    return _rows(df["close"])


def _rows(close: pd.Series) -> list[tuple[datetime, float]]:
    out: list[tuple[datetime, float]] = []
    for idx, value in close.items():
        if pd.isna(value):
            continue
        ts = datetime(idx.year, idx.month, idx.day, tzinfo=IST)
        out.append((ts, float(value)))
    return out


SOURCES = [
    ("NIFTY_50", _read_nifty),
    ("INDIA_VIX", _read_vix),
]


async def _bulk_insert(symbol: str, rows: list[tuple[datetime, float]]) -> None:
    async with conn() as c:
        for i in range(0, len(rows), BATCH):
            chunk = rows[i : i + BATCH]
            await c.executemany(
                """
                INSERT INTO candles (symbol, interval, ts, open, high, low, close, volume)
                VALUES ($1, $2, $3, $4, $4, $4, $4, 0)
                ON CONFLICT (symbol, interval, ts) DO NOTHING
                """,
                [(symbol, INTERVAL, ts, close) for ts, close in chunk],
            )


async def main() -> None:
    await get_pool()
    try:
        for symbol, read_fn in SOURCES:
            rows = read_fn()
            log.info("parsed csv", extra={"symbol": symbol, "rows": len(rows),
                                          "first": str(rows[0][0].date()) if rows else None,
                                          "last": str(rows[-1][0].date()) if rows else None})
            await _bulk_insert(symbol, rows)
            async with conn() as c:
                n = await c.fetchval(
                    "SELECT count(*) FROM candles WHERE symbol = $1 AND interval = $2",
                    symbol, INTERVAL,
                )
            log.info("seeded", extra={"symbol": symbol, "total_1d_bars_in_db": int(n)})
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
