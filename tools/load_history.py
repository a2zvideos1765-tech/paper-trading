"""One-shot loader: bulk-import per-symbol CSVs into the `candles` table.

Two ways to run it:

A. From the VPS (after first checkout, against local Postgres):
       python -m tools.load_history --src ./data/angel_symbols --interval 5m

B. From your Windows machine, against the VPS Postgres over an SSH tunnel
   (see tools/upload_to_vps.ps1, which wraps this and the tunnel):
       python -m tools.load_history --src .\data\angel_symbols --interval 5m \\
           --pg-host 127.0.0.1 --pg-port 6543 --pg-password "$env:PG_PASSWORD"

Idempotent: re-running skips rows that already exist (ON CONFLICT DO NOTHING).

Note: this script intentionally does NOT depend on src.core.config / src.core.db.
Those require Angel + dashboard secrets, which the uploading Windows machine
shouldn't need just to push CSVs.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import pandas as pd
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")  # optional — fine if missing on Windows


REQUIRED_COLS = {"timestamp", "open", "high", "low", "close", "volume"}


def _build_dsn(args: argparse.Namespace) -> str:
    """CLI flag > env var > default. PG_PASSWORD has no default (must be supplied)."""
    host = args.pg_host or os.getenv("PG_HOST", "127.0.0.1")
    port = args.pg_port if args.pg_port is not None else int(os.getenv("PG_PORT", "5432"))
    db = args.pg_db or os.getenv("PG_DB", "paper_trading")
    user = args.pg_user or os.getenv("PG_USER", "paper")
    password = args.pg_password or os.getenv("PG_PASSWORD")
    if not password:
        raise SystemExit(
            "PG_PASSWORD missing. Pass --pg-password, set it in .env, or export PG_PASSWORD."
        )
    return f"postgresql://{user}:{password}@{host}:{int(port)}/{db}"


async def _load_one(pool: asyncpg.Pool, symbol: str, interval: str, df: pd.DataFrame) -> int:
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
    async with pool.acquire() as c:
        await c.executemany(
            """
            INSERT INTO candles (symbol, interval, ts, open, high, low, close, volume)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (symbol, interval, ts) DO NOTHING
            """,
            rows,
        )
    return len(rows)


async def _run(args: argparse.Namespace) -> None:
    src: Path = args.src
    interval: str = args.interval
    dsn = _build_dsn(args)

    csvs = sorted(src.glob("*.csv"))
    if not csvs:
        raise SystemExit(f"No CSVs found in {src}")

    parsed = urlparse(dsn)
    target = f"{(parsed.path or '/').lstrip('/')}@{parsed.hostname}:{parsed.port}"
    print(f"Loading {len(csvs)} files from {src} (interval={interval}) into {target}")

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4, command_timeout=120)
    try:
        total_rows = 0
        for path in csvs:
            symbol = path.stem
            df = pd.read_csv(path)
            missing = REQUIRED_COLS - set(df.columns)
            if missing:
                print(f"  SKIP {symbol}: missing columns {missing}")
                continue
            n = await _load_one(pool, symbol, interval, df)
            total_rows += n
            print(f"  {symbol}: enqueued {n:,} rows")
        print(f"Done. {total_rows:,} candles processed (existing rows are deduped server-side).")
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk-load per-symbol CSVs into the candles table.")
    parser.add_argument("--src", type=Path, required=True,
                        help="Directory of {SYMBOL}.csv files (algo project's data/angel_symbols)")
    parser.add_argument("--interval", default="5m",
                        help="Interval label to record (default 5m, matches algo project's intraday CSVs)")

    db = parser.add_argument_group("DB connection (overrides .env / env vars)")
    db.add_argument("--pg-host", help="default: PG_HOST or 127.0.0.1")
    db.add_argument("--pg-port", type=int, help="default: PG_PORT or 5432")
    db.add_argument("--pg-db", help="default: PG_DB or paper_trading")
    db.add_argument("--pg-user", help="default: PG_USER or paper")
    db.add_argument("--pg-password", help="default: PG_PASSWORD env var (preferred over CLI)")

    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
