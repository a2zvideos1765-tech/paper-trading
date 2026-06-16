"""Re-point universe equities from BSE to their NSE listing.

Real orders must go to the exchange the Angel account is enabled on (NSE) and
must match how the user trades manually (AUROPHARMA-EQ). Symbols were added on
BSE, so the bot placed BSE orders that Angel rejected with an empty response.

This re-points each symbol's token + exchange to its NSE cash listing. The engine
`symbol` is left UNCHANGED (e.g. "AUROPHARMA"), so all existing candles / trades /
positions / engine intents stay valid — only the broker routing changes. The Angel
tradingsymbol the bot sends (e.g. "AUROPHARMA-EQ") is derived from the instruments
master via the NSE token, so it becomes correct automatically.

NSE cash tradingsymbols are "<SYMBOL>-EQ" in the Angel instrument master.

Usage:
    python -m tools.migrate_universe_to_nse --dry-run            # preview every BSE equity
    python -m tools.migrate_universe_to_nse --symbol AUROPHARMA  # one symbol
    python -m tools.migrate_universe_to_nse --all                # every non-NSE equity
    python -m tools.migrate_universe_to_nse --all --backfill     # + queue a 200d NSE backfill

By default candles are NOT re-fetched, so existing engine intents keep their
(BSE-derived) prices — useful when you want the bot to place an already-decided
trade. Pass --backfill to rebuild candles on NSE (changes future intents to
NSE-derived prices); recommended once for the full migration.
"""

from __future__ import annotations

import argparse
import asyncio

from src.core.db import close_pool, conn, get_pool


async def _nse_listing(c, engine_symbol: str):
    """The NSE cash instrument for an engine symbol, or None if not in the master."""
    return await c.fetchrow(
        "SELECT token, symbol, name FROM instruments "
        "WHERE exchange = 'NSE' AND symbol = $1 LIMIT 1",
        f"{engine_symbol}-EQ",
    )


async def run(symbol: str | None, do_all: bool, backfill: bool, dry_run: bool) -> None:
    async with conn() as c:
        if symbol:
            targets = await c.fetch(
                "SELECT symbol, exchange, token FROM universe_symbols "
                "WHERE enabled AND kind = 'equity' AND symbol = $1",
                symbol,
            )
            if not targets:
                print(f"No enabled equity named {symbol!r} in the universe.")
                return
        else:
            targets = await c.fetch(
                "SELECT symbol, exchange, token FROM universe_symbols "
                "WHERE enabled AND kind = 'equity' AND exchange <> 'NSE' "
                "ORDER BY symbol"
            )

        migrated = skipped = 0
        unmatched: list[str] = []

        for t in targets:
            sym, old_exch, old_token = t["symbol"], t["exchange"], t["token"]
            if old_exch == "NSE":
                skipped += 1
                continue

            nse = await _nse_listing(c, sym)
            if not nse:
                unmatched.append(sym)
                print(f"  UNMATCHED  {sym}: no NSE '{sym}-EQ' in instruments "
                      f"(refresh the instrument master, then retry)")
                continue

            tag = "(dry) " if dry_run else ""
            print(f"  {tag}{sym}: {old_exch}/{old_token} -> NSE/{nse['token']} "
                  f"({nse['symbol']})")
            if dry_run:
                continue

            dup = await c.fetchrow(
                "SELECT 1 FROM universe_symbols WHERE symbol = $1 AND exchange = 'NSE'",
                sym,
            )
            if dup:
                # An NSE row already exists — enable it, disable the BSE one.
                await c.execute(
                    "UPDATE universe_symbols SET enabled = TRUE, token = $2 "
                    "WHERE symbol = $1 AND exchange = 'NSE'", sym, nse["token"])
                await c.execute(
                    "UPDATE universe_symbols SET enabled = FALSE "
                    "WHERE symbol = $1 AND exchange = $2", sym, old_exch)
            else:
                await c.execute(
                    "UPDATE universe_symbols SET token = $1, exchange = 'NSE' "
                    "WHERE symbol = $2 AND exchange = $3", nse["token"], sym, old_exch)

            if backfill:
                await c.execute(
                    "INSERT INTO backfill_queue (symbol, exchange, token, interval, days) "
                    "VALUES ($1, 'NSE', $2, '5m', 200)", sym, nse["token"])

            migrated += 1

        print(f"\nmigrated={migrated} skipped(already NSE)={skipped} "
              f"unmatched={len(unmatched)}")
        if unmatched:
            print("unmatched:", ", ".join(unmatched))
        if backfill and migrated:
            print("Queued NSE backfills — run `python -m src.runners.backfill_queue` "
                  "or wait for the 18:00 IST cron.")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Re-point universe equities to NSE.")
    ap.add_argument("--symbol", help="Migrate one engine symbol (e.g. AUROPHARMA).")
    ap.add_argument("--all", action="store_true", help="Migrate every non-NSE equity.")
    ap.add_argument("--backfill", action="store_true",
                    help="Also queue a 200-day NSE backfill (rebuilds candles).")
    ap.add_argument("--dry-run", action="store_true", help="Preview only; no writes.")
    args = ap.parse_args()
    if not args.symbol and not args.all:
        raise SystemExit("Pass --symbol SYM or --all (optionally --dry-run / --backfill).")

    await get_pool()
    try:
        await run(args.symbol, args.all, args.backfill, args.dry_run)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
