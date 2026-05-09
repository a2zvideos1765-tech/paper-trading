"""One-shot loader: bulk-import per-symbol CSVs from the algo project's
`data/angel_symbols/` directory into the `candles` table.

Usage (from the paper-trading repo root, after copying the CSV directory locally):

    python -m tools.load_history --src ./data/angel_symbols --interval 5m

Idempotent: re-running skips rows that already exist (ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import pandas as pd

from src.core.config import settings
from src.core.db import conn, get_pool, close_pool


REQUIRED_COLS = {"timestamp", "open", "high", "low", "close", "volume"}


async def _load_one(symbol: str, interval: str, df: pd.DataFrame) -> int:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is None:
        # Files were saved naive; the algo project treats them as UTC then converts to IST.
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata")
    rows = [
        (symbol, interval, ts.to_pydatetime(),
         float(o), float(h), float(l), float(c), int(v))
        for ts, o, h, l, c, v in df[["timestamp", "open", "high", "low", "close", "volume"]].itertuples(index=False)
    ]
    async with conn() as c:
        # COPY would be faster but executemany + ON CONFLICT keeps idempotency simple.
        await c.executemany(
            """
            INSERT INTO candles (symbol, interval, ts, open, high, low, close, volume)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (symbol, interval, ts) DO NOTHING
            """,
            rows,
        )
    return len(rows)


async def _main(src: Path, interval: str) -> None:
    csvs = sorted(src.glob("*.csv"))
    if not csvs:
        raise SystemExit(f"No CSVs found in {src}")
    print(f"Loading {len(csvs)} files from {src} (interval={interval}) into "
          f"{settings.pg_db}@{settings.pg_host}:{settings.pg_port}")
    total_rows = 0
    for path in csvs:
        symbol = path.stem
        df = pd.read_csv(path)
        missing = REQUIRED_COLS - set(df.columns)
        if missing:
            print(f"  SKIP {symbol}: missing columns {missing}")
            continue
        n = await _load_one(symbol, interval, df)
        total_rows += n
        print(f"  {symbol}: enqueued {n:,} rows")
    print(f"Done. {total_rows:,} candles processed (existing rows are deduped server-side).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk-load per-symbol CSVs into candles table.")
    parser.add_argument("--src", type=Path, required=True,
                        help="Directory of {SYMBOL}.csv files (algo project's data/angel_symbols)")
    parser.add_argument("--interval", default="5m",
                        help="Interval label to record (default 5m, matches algo project's intraday CSVs)")
    args = parser.parse_args()

    try:
        asyncio.run(_run(args.src, args.interval))
    finally:
        pass


async def _run(src: Path, interval: str) -> None:
    try:
        await get_pool()
        await _main(src, interval)
    finally:
        await close_pool()


if __name__ == "__main__":
    main()
