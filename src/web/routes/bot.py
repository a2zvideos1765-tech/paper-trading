"""Bot management page + JSON API for the real-money Angel One runner.

The page at /bot shows:
  - Master ON/OFF toggle (admin only)
  - Live fund balance (available cash / utilised / net)
  - Real holdings mirror from Angel
  - Real-order ledger (recent 50 rows)
  - SIP panel: detected deposits + total deployed, link to live portfolio page

JSON polling endpoints:
  GET  /api/bot/status   — bot state + market open + live portfolio summary
  GET  /api/bot/funds    — latest real_funds snapshot
  GET  /api/bot/holdings — real_holdings rows
  GET  /api/bot/orders   — recent real_orders (limit 50)
  POST /api/bot/toggle   — admin: flip real_bot_state.enabled
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from src.core.db import execute, fetch, fetchrow
from src.core.time import IST, is_market_open, now_ist
from src.web.auth import require_admin


router = APIRouter()


# ---------- helpers ----------

def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).isoformat()


def _flt(v, default=None):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ---------- page ----------

@router.get("/bot", response_class=HTMLResponse)
async def bot_page(request: Request) -> HTMLResponse:
    # Load whatever we need server-side for the initial render; the JS
    # poller handles live updates.
    bot = await fetchrow("SELECT enabled, note, updated_at FROM real_bot_state WHERE id = 1")
    live_pf = await fetchrow(
        "SELECT id, name, strategy_id, capital::float8, enabled, started_at "
        "FROM portfolios WHERE live = TRUE LIMIT 1"
    )
    funds = await fetchrow(
        "SELECT available_cash::float8, net::float8, utilised::float8, as_of "
        "FROM real_funds ORDER BY as_of DESC LIMIT 1"
    )
    deposits = await fetch(
        "SELECT ts, amount::float8, available_before::float8, available_after::float8, note "
        "FROM real_deposits ORDER BY ts DESC"
    )
    total_deposited = sum(float(r["amount"]) for r in deposits)

    return request.app.state.templates.TemplateResponse(
        request, "bot.html",
        {
            "bot_enabled": bool(bot["enabled"]) if bot else False,
            "bot_note": bot["note"] if bot else None,
            "bot_updated_at": bot["updated_at"] if bot else None,
            "live_pf": dict(live_pf) if live_pf else None,
            "funds": dict(funds) if funds else None,
            "deposits": [dict(r) for r in deposits],
            "total_deposited": total_deposited,
            "market_open": is_market_open(),
        },
    )


# ---------- status ----------

@router.get("/api/bot/status")
async def api_bot_status() -> JSONResponse:
    bot = await fetchrow(
        "SELECT enabled, note, updated_at FROM real_bot_state WHERE id = 1"
    )
    beat = await fetchrow(
        "SELECT last_beat, status, detail FROM runs WHERE app = 'real_trader'"
    )
    live_pf = await fetchrow(
        """
        SELECT p.id, p.name, p.strategy_id, p.capital::float8,
               e.equity::float8, e.cash::float8, e.holdings_value::float8
        FROM portfolios p
        LEFT JOIN LATERAL (
            SELECT equity, cash, holdings_value
            FROM equity_snapshots WHERE portfolio_id = p.id
            ORDER BY ts DESC LIMIT 1
        ) e ON TRUE
        WHERE p.live = TRUE LIMIT 1
        """
    )
    return JSONResponse({
        "enabled": bool(bot["enabled"]) if bot else False,
        "note": bot["note"] if bot else None,
        "updated_at": _iso(bot["updated_at"]) if bot else None,
        "market_open": is_market_open(),
        "now_ist": now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        "last_beat": _iso(beat["last_beat"]) if beat else None,
        "runner_status": beat["status"] if beat else None,
        "runner_detail": beat["detail"] if beat else None,
        "portfolio": {
            "id": live_pf["id"],
            "name": live_pf["name"],
            "strategy_id": live_pf["strategy_id"],
            "capital": _flt(live_pf["capital"]),
            "equity": _flt(live_pf["equity"]),
            "cash": _flt(live_pf["cash"]),
            "holdings_value": _flt(live_pf["holdings_value"]),
        } if live_pf else None,
    })


# ---------- funds ----------

@router.get("/api/bot/funds")
async def api_bot_funds() -> JSONResponse:
    row = await fetchrow(
        "SELECT available_cash::float8, net::float8, utilised::float8, as_of "
        "FROM real_funds ORDER BY as_of DESC LIMIT 1"
    )
    if not row:
        return JSONResponse({"available_cash": None, "net": None, "utilised": None, "as_of": None})
    return JSONResponse({
        "available_cash": _flt(row["available_cash"]),
        "net": _flt(row["net"]),
        "utilised": _flt(row["utilised"]),
        "as_of": _iso(row["as_of"]),
    })


# ---------- holdings ----------

@router.get("/api/bot/holdings")
async def api_bot_holdings() -> JSONResponse:
    rows = await fetch(
        "SELECT symbol, qty, avg_price::float8, ltp::float8, pnl::float8, as_of "
        "FROM real_holdings ORDER BY symbol"
    )
    return JSONResponse([
        {
            "symbol": r["symbol"],
            "qty": int(r["qty"]),
            "avg_price": _flt(r["avg_price"]),
            "ltp": _flt(r["ltp"]),
            "pnl": _flt(r["pnl"]),
            "as_of": _iso(r["as_of"]),
        }
        for r in rows
    ])


# ---------- orders ----------

@router.get("/api/bot/orders")
async def api_bot_orders(limit: int = 50) -> JSONResponse:
    limit = max(1, min(limit, 200))
    rows = await fetch(
        """
        SELECT id, symbol, side, qty, order_type, product, requested_price::float8,
               angel_order_id, status, filled_qty, avg_fill_price::float8,
               reason, error, requested_at, updated_at
        FROM real_orders
        ORDER BY requested_at DESC
        LIMIT $1
        """,
        limit,
    )
    return JSONResponse([
        {
            "id": r["id"],
            "symbol": r["symbol"],
            "side": r["side"],
            "qty": r["qty"],
            "order_type": r["order_type"],
            "product": r["product"],
            "requested_price": _flt(r["requested_price"]),
            "angel_order_id": r["angel_order_id"],
            "status": r["status"],
            "filled_qty": r["filled_qty"],
            "avg_fill_price": _flt(r["avg_fill_price"]),
            "reason": r["reason"],
            "error": r["error"],
            "requested_at": _iso(r["requested_at"]),
            "updated_at": _iso(r["updated_at"]),
        }
        for r in rows
    ])


# ---------- deposits ----------

@router.get("/api/bot/deposits")
async def api_bot_deposits() -> JSONResponse:
    rows = await fetch(
        "SELECT ts, amount::float8, available_before::float8, available_after::float8, note "
        "FROM real_deposits ORDER BY ts DESC"
    )
    return JSONResponse([
        {
            "ts": _iso(r["ts"]),
            "amount": _flt(r["amount"]),
            "available_before": _flt(r["available_before"]),
            "available_after": _flt(r["available_after"]),
            "note": r["note"],
        }
        for r in rows
    ])


# ---------- toggle ----------

@router.post("/api/bot/toggle")
async def api_bot_toggle(request: Request) -> JSONResponse:
    require_admin(request)
    try:
        body = await request.json()
    except Exception:  # empty or malformed body → just flip current state
        body = {}
    # Accept explicit {"enabled": true/false} or just flip current state
    if isinstance(body, dict) and "enabled" in body:
        new_state = bool(body["enabled"])
    else:
        cur = await fetchrow("SELECT enabled FROM real_bot_state WHERE id = 1")
        new_state = not bool(cur["enabled"]) if cur else True

    # Upsert so the toggle works even if sql/007's seed row is missing.
    await execute(
        """
        INSERT INTO real_bot_state (id, enabled, updated_at, updated_by)
        VALUES (1, $1, now(), 'web')
        ON CONFLICT (id) DO UPDATE
          SET enabled = $1, updated_at = now(), updated_by = 'web'
        """,
        new_state,
    )
    return JSONResponse({"enabled": new_state, "now_ist": now_ist().strftime("%Y-%m-%d %H:%M:%S IST")})
