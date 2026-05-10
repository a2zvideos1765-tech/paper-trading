"""Per-trade replay debug page.

Given a portfolio_id and a trade ts, shows the trade details + the candle that
triggered it + the strategy parameters in effect. This is where future Claude /
the user goes when "why did/didn't this fire?" comes up.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from src.core.db import fetch, fetchrow
from src.strategies.registry import all_strategies


router = APIRouter()


@router.get("/diagnose/{portfolio_id}/{symbol}/{ts}", response_class=HTMLResponse)
async def diagnose(request: Request, portfolio_id: int, symbol: str, ts: str) -> HTMLResponse:
    """ts is ISO format, URL-encoded if it has a '+'. Example: 2026-05-09T10:30:00+05:30"""
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        raise HTTPException(400, "ts must be ISO 8601, e.g. 2026-05-09T10:30:00+05:30")

    p = await fetchrow(
        "SELECT id, name, strategy_id, capital::float8 FROM portfolios WHERE id = $1",
        portfolio_id,
    )
    if not p:
        raise HTTPException(404, "Portfolio not found")

    strategy = all_strategies().get(p["strategy_id"])

    # Surrounding candles ±30 minutes for context.
    bars = await fetch(
        """
        SELECT ts, open::float8, high::float8, low::float8, close::float8, volume, interval
        FROM candles
        WHERE symbol = $1 AND ts BETWEEN $2 AND $3
        ORDER BY ts
        """,
        symbol, when - timedelta(minutes=30), when + timedelta(minutes=30),
    )

    # Trade(s) at exactly this ts on this symbol for this portfolio.
    trades = await fetch(
        """
        SELECT ts, side, qty, price::float8, charges::float8, cash_after::float8, reason
        FROM trades
        WHERE portfolio_id = $1 AND symbol = $2 AND ts BETWEEN $3 AND $4
        ORDER BY ts
        """,
        portfolio_id, symbol,
        when - timedelta(minutes=5), when + timedelta(minutes=5),
    )

    return request.app.state.templates.TemplateResponse(
        request, "diagnose.html",
        {
            "portfolio": dict(p),
            "strategy": strategy,
            "symbol": symbol,
            "ts": when,
            "bars": [dict(b) for b in bars],
            "trades": [dict(t) for t in trades],
        },
    )
