"""Real-money trading runner (Angel One).

Mirrors src/runners/trader.py, but for the single live S404 portfolio. Each tick
during market hours:

  1. Sync broker state (ALWAYS, even when the bot is OFF):
       - pull funds  → real_funds   (+ detect SIP deposits)
       - pull holdings → real_holdings
  2. If the master switch (real_bot_state.enabled) is OFF → stop here (shadow only).
  3. If ON:
       - replay the engine for the live portfolio (forward-only, with the SIP
         deposits map + the min_entry_cash override) — this persists the engine's
         canonical trades/positions/equity so the normal dashboard/CSV/APY work.
       - diff the engine's intended trades against real_orders; for each NEW intent
         dated today, place a CNC LIMIT order at the engine's decided price.
       - reconcile open real_orders against Angel's order book.
  4. Heartbeat.

Safety: the bot follows the S404 engine output verbatim — no extra caps. The only
gates are the master switch, market hours, and the strategy's own min_entry_cash.
The master switch defaults OFF (sql/007), so deploying this never auto-trades.
"""

from __future__ import annotations

import asyncio
import json
import time as _time
from datetime import date as _date

from src.core.angel import AngelClient
from src.core.config import settings
from src.core.db import close_pool, conn, fetch, fetchrow, get_pool, heartbeat
from src.core.logging import setup_logging
from src.core.time import IST, is_market_open, now_ist, seconds_until_market_open
from src.core.universe import load_universe
from src.engine.real_executor import select_new_intents
from src.engine.replay import (
    PortfolioRow,
    load_candles_window,
    load_index_close,
    load_portfolios,
    replay_one_portfolio,
)
from src.engine.v2_engine import ChargeConfigV2
from src.strategies.registry import get as get_strategy


log = setup_logging("real_trader")
CHARGES = ChargeConfigV2()
CANDLE_INTERVAL = "5m"

# A rise in Angel's available cash of at least this much, not explained by a
# completed SELL since the last snapshot, is recorded as a SIP deposit.
MIN_DEPOSIT_DETECT = 500.0

# Map Angel order-book status strings → our real_orders.status enum.
_TERMINAL = {"complete", "rejected", "cancelled"}


# ---------- Angel session ----------
#
# Angel One's SmartConnect JWT expires daily (typically at midnight IST).
# We handle this two ways:
#
#   1. PROACTIVE: track which IST calendar date the session was created on.
#      If the current IST date differs from _session_date, force a re-login
#      before the next tick (catches the midnight rollover cleanly).
#
#   2. REACTIVE: if any tick raises an auth-related exception (token / session /
#      invalid / unauthorised), _reset_angel() clears the cached client so the
#      next call to _angel() re-authenticates. This is the fallback for mid-day
#      token revocations (e.g. Angel resets the session server-side).
#
# generateSession() itself is blocking (HTTP + TOTP); it runs in a thread so
# the asyncio event loop is never stalled.

_client: AngelClient | None = None
_session_date: _date | None = None   # IST date of the last successful login


async def _angel() -> AngelClient:
    """Return a valid Angel One session, re-logging in when needed.

    Re-logins when:
      • First call (cold start)
      • IST date has rolled over since the last login (daily token expiry)
      • _reset_angel() was called after a reactive auth failure
    """
    global _client, _session_date
    today_ist = now_ist().date()
    if _client is None or _session_date != today_ist:
        if _client is not None:
            log.info("angel session: IST date rolled over — re-authenticating",
                     extra={"session_date": str(_session_date), "today_ist": str(today_ist)})
        _client = await asyncio.to_thread(AngelClient.for_trading)
        _session_date = today_ist
        log.info("angel session established",
                 extra={"session_date": str(today_ist), "account": _client.account})
    return _client


def _reset_angel() -> None:
    """Force re-login on the next _angel() call (reactive auth-failure recovery)."""
    global _client, _session_date
    _client = None
    _session_date = None


# ---------- Small helpers ----------

def _num(value, default: float | None = 0.0) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


async def bot_enabled() -> bool:
    row = await fetchrow("SELECT enabled FROM real_bot_state WHERE id = 1")
    return bool(row and row["enabled"])


async def symbol_map() -> dict[str, dict]:
    """engine symbol (e.g. 'RELIANCE') → {tradingsymbol, token, exchange} for orders.

    universe_symbols holds the engine symbol + Angel token + exchange; the instrument
    master gives the Angel tradingsymbol ('RELIANCE-EQ'). Falls back to the engine
    symbol if the instrument row is missing."""
    rows = await fetch(
        """
        SELECT u.symbol AS engine_symbol, u.token, u.exchange,
               COALESCE(i.symbol, u.symbol) AS tradingsymbol
        FROM universe_symbols u
        LEFT JOIN instruments i ON i.token = u.token
        WHERE u.enabled = TRUE AND u.kind = 'equity'
        """
    )
    return {
        r["engine_symbol"]: {
            "tradingsymbol": r["tradingsymbol"],
            "token": r["token"],
            "exchange": r["exchange"],
        }
        for r in rows
    }


# ---------- Broker-state sync (runs every tick, bot ON or OFF) ----------

async def sync_funds(client: AngelClient) -> dict:
    """Pull RMS funds, detect a SIP deposit vs the previous snapshot, persist a row."""
    raw = await asyncio.to_thread(client.get_funds)
    available = _num(raw.get("availablecash"), 0.0) or 0.0
    net = _num(raw.get("net"), None)
    utilised = _num(raw.get("utiliseddebits"), None)

    prev = await fetchrow(
        "SELECT available_cash::float8 AS available_cash, as_of "
        "FROM real_funds ORDER BY as_of DESC LIMIT 1"
    )
    if prev is not None:
        delta = available - float(prev["available_cash"])
        if delta >= MIN_DEPOSIT_DETECT:
            # Only call it a deposit if no completed SELL since the last snapshot
            # could explain the cash rise.
            sell = await fetchrow(
                "SELECT 1 FROM real_orders "
                "WHERE side = 'SELL' AND status = 'complete' AND updated_at > $1 LIMIT 1",
                prev["as_of"],
            )
            if sell is None:
                await conn_execute(
                    "INSERT INTO real_deposits (amount, available_before, available_after, note) "
                    "VALUES ($1, $2, $3, $4)",
                    delta, float(prev["available_cash"]), available,
                    "auto-detected from available-cash increase",
                )
                log.info("SIP deposit detected", extra={"amount": delta, "available": available})

    await conn_execute(
        "INSERT INTO real_funds (available_cash, net, utilised, raw) VALUES ($1, $2, $3, $4)",
        available, net, utilised, json.dumps(raw),
    )
    return {"available_cash": available, "net": net, "utilised": utilised}


async def sync_holdings(client: AngelClient) -> int:
    """Full-replace the real_holdings mirror from Angel. Returns row count."""
    rows = await asyncio.to_thread(client.get_holdings)
    parsed = []
    for h in rows or []:
        sym = h.get("tradingsymbol") or h.get("symbol")
        if not sym:
            continue
        parsed.append((
            sym,
            int(_num(h.get("quantity"), 0) or 0),
            _num(h.get("averageprice"), 0.0) or 0.0,
            _num(h.get("ltp"), None),
            _num(h.get("profitandloss"), None),
        ))
    async with conn() as c:
        async with c.transaction():
            await c.execute("DELETE FROM real_holdings")
            if parsed:
                await c.executemany(
                    "INSERT INTO real_holdings (symbol, qty, avg_price, ltp, pnl) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    parsed,
                )
    return len(parsed)


async def conn_execute(query: str, *args) -> None:
    async with conn() as c:
        await c.execute(query, *args)


# ---------- SIP deposits map for the engine ----------

async def deposits_map() -> dict[str, float]:
    """Detected SIP deposits keyed by IST trading-day string, for the engine's
    `deposits=` injection. The initial capital is starting_cash, not a deposit."""
    rows = await fetch("SELECT ts, amount::float8 AS amount FROM real_deposits")
    out: dict[str, float] = {}
    for r in rows:
        d = r["ts"].astimezone(IST).date().isoformat()
        out[d] = out.get(d, 0.0) + float(r["amount"])
    return out


# ---------- Order placement + reconciliation (bot ON) ----------

async def existing_intent_keys(portfolio_id: int) -> set[str]:
    rows = await fetch(
        "SELECT intent_key FROM real_orders WHERE portfolio_id = $1", portfolio_id
    )
    return {r["intent_key"] for r in rows}


async def place_new_orders(
    client: AngelClient,
    portfolio: PortfolioRow,
    sym_map: dict[str, dict],
    new_intents: list[tuple[str, dict]],
) -> int:
    """Place a CNC LIMIT order at the engine's decided price for each new intent.
    Records intent (status='pending') BEFORE the API call so a crash can't double-place."""
    placed = 0
    for key, trade in new_intents:
        sym = trade["symbol"]
        meta = sym_map.get(sym)
        if not meta:
            log.warning("no instrument mapping; cannot place order",
                        extra={"symbol": sym, "intent_key": key})
            continue

        # Claim the intent atomically. If the row already exists (a prior tick
        # placed it), ON CONFLICT DO NOTHING returns no row → skip.
        async with conn() as c:
            claimed = await c.fetchrow(
                """
                INSERT INTO real_orders
                    (portfolio_id, intent_key, symbol, side, qty, requested_price, reason, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
                ON CONFLICT (intent_key) DO NOTHING
                RETURNING id
                """,
                portfolio.id, key, sym, trade["side"], int(trade["qty"]),
                float(trade["price"]), trade["reason"],
            )
        if claimed is None:
            continue
        order_row_id = claimed["id"]

        try:
            angel_order_id = await asyncio.to_thread(
                client.place_order,
                meta["tradingsymbol"], meta["token"], meta["exchange"],
                trade["side"], int(trade["qty"]), float(trade["price"]),
            )
            await conn_execute(
                "UPDATE real_orders SET angel_order_id = $1, status = 'open', updated_at = now() "
                "WHERE id = $2",
                angel_order_id, order_row_id,
            )
            await conn_execute(
                "INSERT INTO real_order_events (order_id, status, raw) VALUES ($1, 'open', $2)",
                order_row_id, json.dumps({"angel_order_id": angel_order_id}),
            )
            placed += 1
            log.info("real order placed",
                     extra={"symbol": sym, "side": trade["side"], "qty": trade["qty"],
                            "price": trade["price"], "angel_order_id": angel_order_id})
        except Exception as exc:  # noqa: BLE001
            await conn_execute(
                "UPDATE real_orders SET status = 'error', error = $1, updated_at = now() "
                "WHERE id = $2",
                str(exc)[:300], order_row_id,
            )
            await conn_execute(
                "INSERT INTO real_order_events (order_id, status, raw) VALUES ($1, 'error', $2)",
                order_row_id, json.dumps({"error": str(exc)[:300]}),
            )
            log.exception("real order placement failed",
                          extra={"symbol": sym, "intent_key": key})
    return placed


async def reconcile_open_orders(client: AngelClient, portfolio_id: int) -> None:
    """Poll Angel's order book and update any non-terminal real_orders rows."""
    open_rows = await fetch(
        "SELECT id, angel_order_id, status FROM real_orders "
        "WHERE portfolio_id = $1 AND status IN ('pending', 'open')",
        portfolio_id,
    )
    if not open_rows:
        return
    book = await asyncio.to_thread(client.get_order_book)
    by_id = {str(o.get("orderid")): o for o in book if o.get("orderid")}

    for row in open_rows:
        aid = row["angel_order_id"]
        if not aid or str(aid) not in by_id:
            continue
        o = by_id[str(aid)]
        raw_status = str(o.get("status", "")).strip().lower()
        # Normalise Angel's status vocabulary to ours.
        if raw_status in _TERMINAL:
            status = raw_status
        elif raw_status in ("open", "open pending", "modified", "trigger pending", "validation pending"):
            status = "open"
        else:
            status = "open"
        if status == row["status"]:
            continue
        avg = _num(o.get("averageprice"), None)
        filled = int(_num(o.get("filledshares"), 0) or 0)
        await conn_execute(
            "UPDATE real_orders SET status = $1, avg_fill_price = $2, filled_qty = $3, "
            "error = $4, updated_at = now() WHERE id = $5",
            status, avg, filled, (o.get("text") or None) if status == "rejected" else None, row["id"],
        )
        await conn_execute(
            "INSERT INTO real_order_events (order_id, status, raw) VALUES ($1, $2, $3)",
            row["id"], status, json.dumps(o, default=str),
        )
        log.info("real order updated",
                 extra={"angel_order_id": aid, "status": status, "filled": filled})


# ---------- One tick ----------

async def tick() -> None:
    client = await _angel()

    # 1. Broker-state sync — always, even when the bot is OFF.
    funds = await sync_funds(client)
    held = await sync_holdings(client)

    # 2. Master switch.
    if not await bot_enabled():
        await heartbeat("real_trader", "ok",
                        detail=f"shadow (bot OFF) — cash ₹{funds['available_cash']:,.0f}, {held} holdings")
        return

    # 3. Bot ON — run the engine for the live portfolio(s).
    portfolios = await load_portfolios(live=True)
    if not portfolios:
        await heartbeat("real_trader", "ok", detail="bot ON but no live portfolio")
        return

    equities, _indices = await load_universe()
    equity_symbols = [s.symbol for s in equities]
    sym_map = await symbol_map()
    deposits = await deposits_map()
    today_str = now_ist().date().isoformat()

    until = now_ist().replace(second=0, microsecond=0)
    earliest_start = min(p.started_at for p in portfolios)
    candles = await load_candles_window(equity_symbols, CANDLE_INTERVAL, earliest_start, until)
    nifty = await load_index_close("NIFTY_50", interval="1d")
    sensex = await load_index_close("SENSEX", interval="1d")
    vix = await load_index_close("INDIA_VIX", interval="1d")

    total_placed = 0
    for p in portfolios:
        try:
            strategy = get_strategy(p.strategy_id)
            p_candles = candles if candles.empty else candles[candles["timestamp"] >= p.started_at]
            # Persist the engine's canonical trades/positions/equity so the normal
            # portfolio dashboard/CSV/APY surfaces work for the live portfolio too.
            result = await replay_one_portfolio(
                p, strategy, p_candles, CHARGES, nifty, sensex, vix,
                record_intraday=True, deposits=deposits or None,
            )
            if result.get("validation_errors"):
                continue

            # Diff intents → place real orders for today's new signals.
            existing = await existing_intent_keys(p.id)
            new_intents = select_new_intents(result["trades"], existing, today_str)
            total_placed += await place_new_orders(client, p, sym_map, new_intents)

            # Reconcile any still-open orders against the broker.
            await reconcile_open_orders(client, p.id)
        except Exception:  # noqa: BLE001
            log.exception("live portfolio tick failed",
                          extra={"portfolio_id": p.id, "portfolio_name": p.name})

    await heartbeat("real_trader", "ok",
                    detail=f"bot ON — {len(portfolios)} live pf, {total_placed} new order(s), "
                           f"cash ₹{funds['available_cash']:,.0f}")


# ---------- Loop ----------

async def main() -> None:
    await get_pool()
    log.info("real_trader starting",
             extra={"tick_seconds": settings.trader_interval_seconds, "mode": "live (master switch gated)"})

    # Offset slightly after the poller so candles for this minute are written first.
    await asyncio.sleep(settings.trader_offset_seconds)

    try:
        while True:
            if not is_market_open():
                wait = max(60.0, min(seconds_until_market_open(), 1800.0))
                await heartbeat("real_trader", "sleeping", detail="market closed")
                log.info("market closed, sleeping", extra={"wait_seconds": wait})
                await asyncio.sleep(wait)

                # When we wake up close to (or at) market open, pre-emptively
                # re-authenticate so the FIRST tick of the day doesn't carry
                # a stale yesterday-dated JWT. _angel() will re-login whenever
                # the IST date has changed, so this is a no-op if we somehow
                # wake within the same calendar day.
                if is_market_open():
                    try:
                        await _angel()
                        log.info("pre-market re-authentication OK",
                                 extra={"session_date": str(_session_date)})
                    except Exception as exc:  # noqa: BLE001
                        log.exception("pre-market re-authentication failed — will retry on first tick",
                                      extra={"error": str(exc)[:200]})
                        _reset_angel()
                continue

            cycle_start = _time.monotonic()
            try:
                await tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("tick errored")
                # Reactive: a session/token error clears the cached client so
                # the next tick re-authenticates automatically via _angel().
                if any(w in str(exc).lower() for w in ("token", "session", "invalid", "unauthor")):
                    _reset_angel()
                await heartbeat("real_trader", "error", detail=str(exc)[:200])

            elapsed = _time.monotonic() - cycle_start
            await asyncio.sleep(max(0.0, settings.trader_interval_seconds - elapsed))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
