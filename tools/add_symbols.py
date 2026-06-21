"""Bulk-add NSE equities to the universe by symbol name.

For each name, resolves `<SYMBOL>-EQ` on NSE from the instrument master, inserts
(or re-enables) it in universe_symbols, and queues a 200-day 5m backfill. Names
may be given bare (TDPOWERSYS), Yahoo-style (TDPOWERSYS.NS), or with the suffix
(TDPOWERSYS-EQ) — all normalised to the NSE base symbol.

Run on the VPS:
    python -m tools.add_symbols TDPOWERSYS ELGIEQUIP CAPLIPOINT SARDAEN SANSERA \
        INOXINDIA GABRIEL GALLANTT THANGAMAYL BIKAJI SHRIPISTON FINEORG
    python -m tools.add_symbols --dry-run TDPOWERSYS ...     # preview, no writes
    python -m tools.add_symbols --no-backfill TDPOWERSYS ... # add without queuing history

Note: the universe is shared by all portfolios, so added names become eligible
for the LIVE bot too (real entries) once their candles backfill. If the master
lacks a name, refresh it first: python -m tools.refresh_instruments
"""

from __future__ import annotations

import argparse
import asyncio

from src.core.db import close_pool, conn, get_pool


def _normalize(raw: str) -> str:
    """TDPOWERSYS.NS / TDPOWERSYS-EQ / tdpowersys → TDPOWERSYS (NSE base symbol)."""
    s = raw.strip().upper()
    for suffix in (".NS", ".NSE", "-EQ"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s


async def run(symbols: list[str], backfill: bool, dry_run: bool) -> None:
    async with conn() as c:
        added: list[str] = []
        unmatched: list[str] = []
        for raw in symbols:
            sym = _normalize(raw)
            if not sym:
                continue
            inst = await c.fetchrow(
                "SELECT token, symbol, name FROM instruments "
                "WHERE exchange = 'NSE' AND symbol = $1 LIMIT 1",
                f"{sym}-EQ",
            )
            if not inst:
                unmatched.append(sym)
                print(f"  UNMATCHED  {sym}: no NSE '{sym}-EQ' in instruments "
                      f"(run tools.refresh_instruments, then retry)")
                continue

            existing = await c.fetchrow(
                "SELECT enabled FROM universe_symbols WHERE symbol = $1 AND exchange = 'NSE'",
                sym,
            )
            note = " [already present]" if existing else ""
            print(f"  {'(dry) ' if dry_run else ''}{sym}: NSE token {inst['token']} "
                  f"({inst['name']}){note}")
            if dry_run:
                continue

            await c.execute(
                """
                INSERT INTO universe_symbols (symbol, exchange, token, kind, enabled)
                VALUES ($1, 'NSE', $2, 'equity', TRUE)
                ON CONFLICT (symbol, exchange) DO UPDATE
                  SET enabled = TRUE, token = EXCLUDED.token, kind = 'equity'
                """,
                sym, inst["token"],
            )
            if backfill:
                await c.execute(
                    "INSERT INTO backfill_queue (symbol, exchange, token, interval, days) "
                    "VALUES ($1, 'NSE', $2, '5m', 200)",
                    sym, inst["token"],
                )
            added.append(sym)

        print(f"\nadded={len(added)} unmatched={len(unmatched)}")
        if unmatched:
            print("unmatched:", ", ".join(unmatched))
        if backfill and added:
            print("Backfills queued — run `python -m src.runners.backfill_queue` "
                  "or wait for the 18:00 IST cron.")


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Bulk-add NSE equities to the universe.")
    ap.add_argument("symbols", nargs="+", help="NSE symbols (bare / .NS / -EQ)")
    ap.add_argument("--no-backfill", action="store_true", help="don't queue history backfill")
    ap.add_argument("--dry-run", action="store_true", help="preview only; no writes")
    return ap.parse_args()


async def main(args: argparse.Namespace) -> None:
    await get_pool()
    try:
        await run(args.symbols, not args.no_backfill, args.dry_run)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main(_parse_args()))
