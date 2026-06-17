"""One-off manual test: prove real order placement works, without a real fill.

Places a single LIMIT BUY *3% below the live price* (so it won't fill in the
brief window before cancellation), confirms Angel accepts it (returns an order
id), then cancels it. Use this to verify placement after registering the API IP.

⚠️  This touches the LIVE Angel account. It is a real — but cancellable, and
deliberately un-fillable — order for a tiny quantity. Run it yourself:

    python -m tools.test_place_order                       # 1 share AUROPHARMA
    python -m tools.test_place_order --symbol SURANAT&P    # a cheaper scrip
    python -m tools.test_place_order --qty 1 --buffer 0.05 # 5% below market
    python -m tools.test_place_order --no-cancel           # leave it open; cancel in Angel

Note: this logs in a fresh Angel session, which can invalidate the real-trader's
current session. That's fine — the real-trader re-authenticates on its next tick.
"""

from __future__ import annotations

import argparse
import asyncio
import time

from src.core.angel import AngelClient
from src.core.db import close_pool, fetchrow, get_pool


async def _resolve(symbol: str):
    return await fetchrow(
        """
        SELECT u.symbol AS engine_symbol, u.token, u.exchange,
               COALESCE(i.symbol, u.symbol) AS tradingsymbol
        FROM universe_symbols u
        LEFT JOIN instruments i ON i.token = u.token AND i.exchange = u.exchange
        WHERE u.symbol = $1 AND u.enabled = TRUE AND u.kind = 'equity'
        LIMIT 1
        """,
        symbol,
    )


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Place + cancel a tiny test order on Angel.")
    ap.add_argument("--symbol", default="AUROPHARMA", help="universe engine symbol")
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--buffer", type=float, default=0.03,
                    help="fraction below LTP for the BUY limit (default 0.03 = 3%)")
    ap.add_argument("--no-cancel", action="store_true", help="leave the order open")
    return ap.parse_args()


async def main(args: argparse.Namespace) -> None:
    await get_pool()
    try:
        meta = await _resolve(args.symbol)
        if not meta:
            print(f"{args.symbol!r} is not an enabled equity in the universe.")
            return
        ts, token, exch = meta["tradingsymbol"], meta["token"], meta["exchange"]
        print(f"Symbol {args.symbol}: tradingsymbol={ts} token={token} exchange={exch}")

        client = await asyncio.to_thread(AngelClient.for_trading)
        print(f"Logged in on Angel account {client.account}")

        ltp = await asyncio.to_thread(client.get_ltp, exch, ts, token)
        print(f"LTP = {ltp}")
        if not ltp:
            print("No LTP returned; aborting (market closed or symbol issue).")
            return

        price = round(ltp * (1.0 - args.buffer), 2)
        print(f"\nPlacing BUY LIMIT {args.qty} {ts} @ {price} "
              f"({args.buffer*100:.0f}% below LTP — should NOT fill)…")
        try:
            order_id = await asyncio.to_thread(
                client.place_order, ts, token, exch, "BUY", args.qty, price,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"\n❌ placeOrder FAILED: {exc}")
            print("   (If this says AG7002, the IP still isn't registered on this app's key.)")
            return

        print(f"\n✅ ACCEPTED — Angel order id: {order_id}")

        time.sleep(1.5)
        try:
            book = await asyncio.to_thread(client.get_order_book)
            for o in book:
                if str(o.get("orderid")) == str(order_id):
                    print(f"   order-book status: {o.get('status')!r}  text={o.get('text')!r}")
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"   (could not read order book: {exc})")

        if args.no_cancel:
            print("\nLeft the order OPEN (--no-cancel). Cancel it in your Angel app.")
        else:
            try:
                res = await asyncio.to_thread(client.cancel_order, order_id)
                print(f"\nCancel requested: {res}")
                print("Verify it shows CANCELLED in your Angel order book.")
            except Exception as exc:  # noqa: BLE001
                print(f"\n⚠️  cancel failed: {exc}")
                print("   Cancel the order MANUALLY in your Angel app to be safe.")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main(_parse_args()))
