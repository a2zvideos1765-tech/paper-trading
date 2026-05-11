"""Per-portfolio detail view: holdings, trade history, equity curve."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from src.core.db import fetch, fetchrow


router = APIRouter()


@router.get("/portfolio/{portfolio_id}", response_class=HTMLResponse)
async def portfolio_detail(request: Request, portfolio_id: int) -> HTMLResponse:
    p = await fetchrow(
        "SELECT id, name, strategy_id, capital::float8, enabled, started_at "
        "FROM portfolios WHERE id = $1",
        portfolio_id,
    )
    if not p:
        raise HTTPException(404, "Portfolio not found")

    eq_now = await fetchrow(
        """
        SELECT cash::float8, holdings_value::float8, equity::float8, open_positions
        FROM equity_snapshots WHERE portfolio_id = $1 ORDER BY ts DESC LIMIT 1
        """,
        portfolio_id,
    )

    positions = await fetch(
        """
        SELECT pos.symbol, pos.qty, pos.avg_price::float8, pos.entry_price::float8,
               pos.entry_date, pos.peak_price::float8, pos.tiers_hit, pos.pyramid_adds_hit,
               c.close::float8 AS last_close
        FROM positions pos
        LEFT JOIN LATERAL (
            SELECT close FROM candles
            WHERE symbol = pos.symbol
            ORDER BY ts DESC LIMIT 1
        ) c ON TRUE
        WHERE pos.portfolio_id = $1
        ORDER BY pos.symbol
        """,
        portfolio_id,
    )

    holdings = []
    for row in positions:
        last = float(row["last_close"]) if row["last_close"] is not None else float(row["avg_price"])
        market_value = float(row["qty"]) * last
        cost_basis = float(row["qty"]) * float(row["avg_price"])
        pnl = market_value - cost_basis
        pnl_pct = (last / float(row["avg_price"]) - 1) * 100 if row["avg_price"] else 0.0
        holdings.append({
            "symbol": row["symbol"],
            "qty": int(row["qty"]),
            "avg_price": float(row["avg_price"]),
            "last": last,
            "market_value": market_value,
            "cost_basis": cost_basis,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "entry_date": row["entry_date"],
            "tiers_hit": row["tiers_hit"],
            "pyramid_adds_hit": row["pyramid_adds_hit"],
        })

    recent_trades = await fetch(
        """
        SELECT ts, symbol, side, qty, price::float8, charges::float8,
               cash_after::float8, reason
        FROM trades WHERE portfolio_id = $1 ORDER BY ts DESC LIMIT 50
        """,
        portfolio_id,
    )

    return request.app.state.templates.TemplateResponse(
        request, "portfolio.html",
        {
            "portfolio": dict(p),
            "eq": dict(eq_now) if eq_now else {
                "cash": float(p["capital"]),
                "holdings_value": 0.0,
                "equity": float(p["capital"]),
                "open_positions": 0,
            },
            "holdings": holdings,
            "recent_trades": [dict(t) for t in recent_trades],
        },
    )
