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
from src.engine.real_executor import (
    count_stale_intents,
    reconcile_sell_qty,
    scan_time_elapsed,
    select_new_intents,
    sip_deposit_amount,
    surveillance_reject_code,
)
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
               COALESCE(i.symbol, u.symbol) AS tradingsymbol,
               i.tick_size
        FROM universe_symbols u
        LEFT JOIN instruments i ON i.token = u.token AND i.exchange = u.exchange
        WHERE u.enabled = TRUE AND u.kind = 'equity'
        """
    )
    return {
        r["engine_symbol"]: {
            "tradingsymbol": r["tradingsymbol"],
            "token": r["token"],
            "exchange": r["exchange"],
            # instruments.tick_size is in paise → rupees; fall back to ₹0.05.
            "tick": (float(r["tick_size"]) / 100.0) if r["tick_size"] else 0.05,
        }
        for r in rows
    }


# ---------- Broker ↔ engine reconciliation (single source of truth = the broker) ----------

async def broker_holdings_by_engine(reverse_map: dict[str, str]) -> dict[str, dict]:
    """Current broker holdings keyed by ENGINE symbol (e.g. 'INFY', not 'INFY-EQ').

    `reverse_map` is tradingsymbol → engine symbol (built from symbol_map). Only
    universe-mapped holdings are returned — the engine can only manage symbols it has
    candles/features for. Non-universe manual buys are intentionally excluded here (they
    still show on /bot, just unmanaged)."""
    rows = await fetch("SELECT symbol, qty, avg_price::float8 AS avg_price FROM real_holdings")
    out: dict[str, dict] = {}
    for r in rows:
        eng = reverse_map.get(r["symbol"])
        if not eng:
            continue
        q = int(r["qty"] or 0)
        if q <= 0:
            continue
        cur = out.get(eng)
        if cur:  # two broker rows mapping to one engine symbol — sum (defensive)
            cur["qty"] += q
        else:
            out[eng] = {"qty": q, "avg_price": float(r["avg_price"])}
    return out


async def external_positions_map() -> dict[str, dict]:
    """Adopted broker positions keyed by IST first-seen date → {symbol: {qty, avg_price}},
    in the shape run_backtest_v2's `external_positions=` expects. These are positions the
    account holds that the engine didn't create (manual buys / orphaned fills), recorded so
    the engine adopts and exits them per the strategy."""
    rows = await fetch(
        "SELECT symbol, first_seen_date, entry_price::float8 AS entry_price, qty "
        "FROM real_external_positions"
    )
    out: dict[str, dict] = {}
    for r in rows:
        d = r["first_seen_date"]
        dstr = d.isoformat() if hasattr(d, "isoformat") else str(d)
        out.setdefault(dstr, {})[r["symbol"]] = {
            "qty": int(r["qty"]), "avg_price": float(r["entry_price"]),
        }
    return out


async def reconcile_external_positions(
    engine_open_syms: set[str],
    broker_by_engine: dict[str, dict],
    today_str: str,
) -> int:
    """Keep `real_external_positions` in step with reality. Returns count newly adopted.

    Adopt: a universe symbol the BROKER holds but the engine is NOT managing (absent from
    its open positions) — a manual buy or an orphaned fill of an already-closed position.
    Recorded with first-seen date/price/qty as the engine's entry snapshot (ON CONFLICT DO
    NOTHING preserves the original snapshot so the deterministic replay stays stable).

    Release: drop adopted rows the broker no longer holds (fully exited)."""
    adopted = 0
    for eng, info in broker_by_engine.items():
        if eng in engine_open_syms:
            continue  # already managed by the engine — not an external position
        row = await fetchrow(
            "INSERT INTO real_external_positions (symbol, first_seen_date, entry_price, qty, note) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (symbol) DO NOTHING RETURNING symbol",
            eng, _date.fromisoformat(today_str), info["avg_price"], int(info["qty"]),
            "auto-adopted: broker holds, engine did not create",
        )
        if row is not None:
            adopted += 1
            log.info("adopted external position",
                     extra={"symbol": eng, "qty": info["qty"], "avg_price": info["avg_price"]})
    held = list(broker_by_engine.keys())
    if held:
        await conn_execute(
            "DELETE FROM real_external_positions WHERE symbol <> ALL($1::text[])", held)
    else:
        await conn_execute("DELETE FROM real_external_positions")
    return adopted


# ---------- Broker-state sync (runs every tick, bot ON or OFF) ----------

async def sync_funds(client: AngelClient) -> dict:
    """Pull RMS funds, detect a SIP deposit vs the previous snapshot, persist a row."""
    raw = await asyncio.to_thread(client.get_funds)

    # A failed/empty funds read (auth hiccup, rate limit) returns {} → no
    # 'availablecash' key. Writing a 0.0 snapshot here is what fabricated the
    # ~₹19k phantom deposit: the next real reading looked like a giant deposit.
    # So skip the write entirely and reuse the last good snapshot for this tick.
    raw_cash = raw.get("availablecash") if isinstance(raw, dict) else None
    if raw_cash is None:
        log.warning("funds read returned no availablecash — skipping funds write this tick",
                    extra={"raw": str(raw)[:200]})
        last = await fetchrow(
            "SELECT available_cash::float8 AS available_cash, net::float8 AS net, "
            "utilised::float8 AS utilised FROM real_funds ORDER BY as_of DESC LIMIT 1"
        )
        if last is not None:
            return {"available_cash": float(last["available_cash"]),
                    "net": last["net"], "utilised": last["utilised"]}
        return {"available_cash": 0.0, "net": None, "utilised": None}

    available = float(_num(raw_cash, 0.0) or 0.0)
    net = _num(raw.get("net"), None)
    utilised = _num(raw.get("utiliseddebits"), None)

    # SIP deposit detection is net-value based: a deposit is money that lifts the
    # account ABOVE its starting capital + deposits already recorded. The initial
    # funding that *establishes* the capital is not a deposit (counting it on top
    # of the seeded capital is what fabricated the ~₹19k phantom deposit).
    hv = await fetchrow(
        "SELECT COALESCE(SUM(qty * COALESCE(ltp, avg_price)), 0)::float8 AS v FROM real_holdings"
    )
    holdings_value = float(hv["v"]) if hv else 0.0
    account_net = available + holdings_value

    base = await fetchrow(
        "SELECT (COALESCE((SELECT SUM(capital) FROM portfolios WHERE live AND enabled), 0) "
        "      + COALESCE((SELECT SUM(amount) FROM real_deposits), 0))::float8 AS b"
    )
    expected_baseline = float(base["b"]) if base else 0.0

    dep = sip_deposit_amount(account_net, expected_baseline, MIN_DEPOSIT_DETECT)
    if dep > 0.0:
        await conn_execute(
            "INSERT INTO real_deposits (amount, available_before, available_after, note) "
            "VALUES ($1, $2, $3, $4)",
            dep, expected_baseline, account_net,
            "auto-detected: net value above starting capital + prior deposits",
        )
        log.info("SIP deposit detected",
                 extra={"amount": dep, "account_net": account_net, "baseline": expected_baseline})

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


# ---------- Surveillance quarantine (AB4036 auto-skip) ----------

async def active_quarantine_symbols() -> set[str]:
    """Engine symbols currently benched after a surveillance/cautionary rejection
    (e.g. AB4036). Rows past their window are cleared first, so a scrip that leaves
    exchange surveillance becomes tradeable again automatically — no manual upkeep."""
    await conn_execute("DELETE FROM real_quarantine WHERE expires_at <= now()")
    rows = await fetch("SELECT symbol FROM real_quarantine WHERE expires_at > now()")
    return {r["symbol"] for r in rows}


async def quarantine_symbol(symbol: str, code: str, text: str | None, months: int = 3) -> None:
    """Bench a symbol for `months` after a hard broker block. Re-hitting it while benched
    refreshes the window and bumps the hit count; it auto-clears once `expires_at` passes."""
    await conn_execute(
        """
        INSERT INTO real_quarantine (symbol, reason_code, reason_text, quarantined_at, expires_at, hits)
        VALUES ($1, $2, $3, now(), now() + make_interval(months => $4), 1)
        ON CONFLICT (symbol) DO UPDATE SET
            reason_code    = EXCLUDED.reason_code,
            reason_text    = EXCLUDED.reason_text,
            quarantined_at = now(),
            expires_at     = now() + make_interval(months => $4),
            hits           = real_quarantine.hits + 1
        """,
        symbol, code, (text or "")[:300], months,
    )


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
    available_cash: float | None = None,
    engine_target_qty: dict[str, int] | None = None,
    broker_qty: dict[str, int] | None = None,
    quarantined: set[str] | None = None,
) -> int:
    """Place a CNC LIMIT order at the engine's decided price for each new intent.
    Records intent (status='pending') BEFORE the API call so a crash can't double-place.

    `available_cash` is the real Angel free cash this tick. BUYs are gated against
    it (with a small charges buffer) and the remaining cash is reserved per order,
    so the bot never fires an order the account can't fund — the engine sizes off
    *simulated* cash, which can exceed the real balance after rejects/partials.

    SELLs are bound to the BROKER (the single source of truth):
      * `broker_qty` (engine symbol → real held qty) is the hard ceiling.
        Broker holds none → skip entirely (kills phantom sells of never-filled /
        surveillance positions, e.g. PARACABLES rejecting forever).
      * `engine_target_qty` (engine symbol → post-replay position qty) tells us whether
        the engine fully closed the symbol. On a full close (symbol absent), we sweep the
        *entire* broker quantity — clearing duplicate-fill orphans (e.g. broker 6 vs engine
        3). On a partial tier, we sell min(engine qty, broker available) so we never oversell.
      * `sell_reserved` tracks shares already committed THIS tick so multiple tiers for one
        symbol can't collectively oversell."""
    placed = 0
    cash_left = available_cash if available_cash is not None else float("inf")
    sell_reserved: dict[str, int] = {}
    for key, trade in new_intents:
        sym = trade["symbol"]
        meta = sym_map.get(sym)
        if not meta:
            log.warning("no instrument mapping; cannot place order",
                        extra={"symbol": sym, "intent_key": key})
            continue

        is_buy = str(trade["side"]).upper() == "BUY"

        if is_buy:
            # Quarantine guard: a symbol the broker hard-blocks (surveillance/cautionary,
            # e.g. AB4036) is benched for 3 months — skip its BUYs entirely so we don't
            # re-fire a guaranteed-failed order on every signal. SELLs are unaffected, so a
            # genuinely held position can still be exited.
            if quarantined and sym in quarantined:
                log.info("skip BUY — symbol quarantined (surveillance/cautionary block)",
                         extra={"symbol": sym, "reason": trade.get("reason")})
                continue
            # Real-cash guard: skip — don't even claim — an order the account can't
            # afford. ~0.4% buffer for brokerage/STT/stamp/GST.
            eff_qty = int(trade["qty"])
            cost = eff_qty * float(trade["price"]) * 1.004
            if cost > cash_left:
                log.info("skip BUY — insufficient real cash",
                         extra={"symbol": sym, "cost": round(cost, 2), "cash_left": round(cash_left, 2)})
                continue
        else:
            # Broker-bound SELL: never sell more than the account holds; never
            # phantom-sell; sweep orphans on full close. (Pure decision in real_executor.)
            cost = 0.0
            bq = (broker_qty or {}).get(sym, 0)
            fully_closed = engine_target_qty is None or sym not in engine_target_qty
            eff_qty = reconcile_sell_qty(
                int(trade["qty"]), bq, reserved=sell_reserved.get(sym, 0), fully_closed=fully_closed)
            if eff_qty <= 0:
                log.info("skip SELL — broker holds none (phantom / over-sell guard)",
                         extra={"symbol": sym, "broker_qty": bq, "reason": trade.get("reason")})
                continue
            if eff_qty != int(trade["qty"]):
                log.info("SELL qty reconciled to broker",
                         extra={"symbol": sym, "engine_qty": int(trade["qty"]), "broker_qty": bq,
                                "placed_qty": eff_qty, "full_close": fully_closed})

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
                portfolio.id, key, sym, trade["side"], eff_qty,
                float(trade["price"]), trade["reason"],
            )
        if claimed is None:
            continue
        order_row_id = claimed["id"]

        try:
            angel_order_id = await asyncio.to_thread(
                client.place_order,
                meta["tradingsymbol"], meta["token"], meta["exchange"],
                trade["side"], eff_qty, float(trade["price"]),
                tick_size=meta.get("tick", 0.05),
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
            if is_buy:
                cash_left -= cost  # reserve so later BUYs this tick don't over-commit
            else:
                sell_reserved[sym] = sell_reserved.get(sym, 0) + eff_qty  # don't oversell across tiers
            log.info("real order placed",
                     extra={"symbol": sym, "side": trade["side"], "qty": eff_qty,
                            "price": trade["price"], "angel_order_id": angel_order_id})
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            await conn_execute(
                "UPDATE real_orders SET status = 'error', error = $1, updated_at = now() "
                "WHERE id = $2",
                err[:300], order_row_id,
            )
            await conn_execute(
                "INSERT INTO real_order_events (order_id, status, raw) VALUES ($1, 'error', $2)",
                order_row_id, json.dumps({"error": err[:300]}),
            )
            log.exception("real order placement failed",
                          extra={"symbol": sym, "intent_key": key})
            # Auto-skip: if a BUY was hard-blocked (exchange surveillance / cautionary, e.g.
            # AB4036), bench the symbol for 3 months so we stop re-firing a doomed order every
            # signal. Auto-clears after the window if the scrip leaves surveillance.
            if is_buy:
                qcode = surveillance_reject_code(err)
                if qcode:
                    await quarantine_symbol(sym, qcode, err, months=3)
                    log.warning("symbol quarantined after surveillance rejection",
                                extra={"symbol": sym, "code": qcode, "months": 3})
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
    # Holdings first: sync_funds uses the holdings market value to compute the
    # account's net value for SIP-deposit detection, so it must be current.
    held = await sync_holdings(client)
    funds = await sync_funds(client)

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

    # Broker = single source of truth. Map broker holdings to engine symbols, and load the
    # adopted-positions map so the engine manages anything the account holds that it didn't
    # create (manual buys / orphaned fills). Built once per tick from the just-synced state.
    reverse_map = {meta["tradingsymbol"]: eng for eng, meta in sym_map.items()}
    broker_by_engine = await broker_holdings_by_engine(reverse_map)
    broker_qty = {eng: info["qty"] for eng, info in broker_by_engine.items()}
    external_map = await external_positions_map()

    # Mark the engine's simulated cash to the broker's REAL free cash this tick so entry
    # sizing reflects the actual account, absorbing manual sells / withdrawals the stateless
    # replay can't see. One account-wide free-cash figure (single live portfolio).
    broker_cash = funds.get("available_cash")
    cash_override = {today_str: float(broker_cash)} if broker_cash is not None else None

    # Symbols benched after a surveillance/cautionary block (AB4036) — their BUYs are skipped
    # until the 3-month window lapses (auto-clears if the scrip leaves surveillance).
    quarantined = await active_quarantine_symbols()

    until = now_ist().replace(second=0, microsecond=0)
    earliest_start = min(p.started_at for p in portfolios)
    candles = await load_candles_window(equity_symbols, CANDLE_INTERVAL, earliest_start, until)
    nifty = await load_index_close("NIFTY_50", interval="1d")
    sensex = await load_index_close("SENSEX", interval="1d")
    vix = await load_index_close("INDIA_VIX", interval="1d")

    total_placed = 0
    total_stale = 0
    for p in portfolios:
        try:
            strategy = get_strategy(p.strategy_id)
            p_candles = candles if candles.empty else candles[candles["timestamp"] >= p.started_at]
            # Persist the engine's canonical trades/positions/equity so the normal
            # portfolio dashboard/CSV/APY surfaces work for the live portfolio too.
            result = await replay_one_portfolio(
                p, strategy, p_candles, CHARGES, nifty, sensex, vix,
                record_intraday=True, deposits=deposits or None,
                external_positions=external_map or None,
                cash_override=cash_override,
            )
            if result.get("validation_errors"):
                continue

            # Adopt broker positions the engine isn't managing (manual buys / orphaned
            # fills) so the NEXT replay exits them per strategy; release fully-exited ones.
            engine_open_syms = {op["symbol"] for op in result["open_positions"]}
            await reconcile_external_positions(engine_open_syms, broker_by_engine, today_str)
            engine_target_qty = {op["symbol"]: int(op["qty"]) for op in result["open_positions"]}

            # Diff intents → place real orders for recent new signals (within the
            # configured age window — absorbs signals whose candle arrived late).
            existing = await existing_intent_keys(p.id)
            max_age = settings.real_trader_intent_max_age_days
            new_intents = select_new_intents(result["trades"], existing, today_str, max_age_days=max_age)

            # Don't act on TODAY's scan entries until their scan time has passed —
            # before then the engine is evaluating the scan on a provisional latest
            # bar (e.g. an 11:20 bar standing in for the 14:00 scan), so the price
            # isn't final. Past days are already complete, so they're not gated.
            now_hhmm = now_ist().strftime("%H:%M")
            ready, provisional = [], 0
            for key, trade in new_intents:
                if str(trade["date"]) == today_str and not scan_time_elapsed(trade["reason"], now_hhmm):
                    provisional += 1
                    continue
                ready.append((key, trade))
            if provisional:
                log.info("scan entries waiting for scan time (provisional, not placed)",
                         extra={"portfolio_id": p.id, "count": provisional, "now": now_hhmm})

            total_placed += await place_new_orders(
                client, p, sym_map, ready, available_cash=funds.get("available_cash"),
                engine_target_qty=engine_target_qty, broker_qty=broker_qty,
                quarantined=quarantined)

            # Surface signals too old to place, so a skipped entry is visible
            # rather than looking like a silent miss.
            stale = count_stale_intents(result["trades"], existing, today_str, max_age_days=max_age)
            total_stale += stale
            if stale:
                log.info("stale signals skipped (older than placement window)",
                         extra={"portfolio_id": p.id, "portfolio_name": p.name,
                                "count": stale, "max_age_days": max_age})

            # Reconcile any still-open orders against the broker.
            await reconcile_open_orders(client, p.id)
        except Exception:  # noqa: BLE001
            log.exception("live portfolio tick failed",
                          extra={"portfolio_id": p.id, "portfolio_name": p.name})

    stale_note = f", {total_stale} stale skipped" if total_stale else ""
    await heartbeat("real_trader", "ok",
                    detail=f"bot ON — {len(portfolios)} live pf, {total_placed} new order(s)"
                           f"{stale_note}, cash ₹{funds['available_cash']:,.0f}")


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
