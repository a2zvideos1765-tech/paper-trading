"""JSON API for HTMX polling and the equity curve chart."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src.core.db import fetch, fetchrow


router = APIRouter(prefix="/api")


@router.get("/portfolio/{portfolio_id}/state")
async def portfolio_state(portfolio_id: int) -> JSONResponse:
    """HTMX polls this for the live equity / day P&L bar at the top of the
    portfolio page. Light query, runs every 15s."""
    p = await fetchrow(
        "SELECT id, name, capital::float8 FROM portfolios WHERE id = $1",
        portfolio_id,
    )
    if not p:
        raise HTTPException(404)
    eq = await fetchrow(
        """
        SELECT cash::float8, holdings_value::float8, equity::float8, open_positions, ts
        FROM equity_snapshots WHERE portfolio_id = $1 ORDER BY ts DESC LIMIT 1
        """,
        portfolio_id,
    )
    return JSONResponse({
        "id": p["id"], "name": p["name"], "capital": p["capital"],
        "cash":           eq["cash"]           if eq else p["capital"],
        "holdings_value": eq["holdings_value"] if eq else 0.0,
        "equity":         eq["equity"]         if eq else p["capital"],
        "open_positions": eq["open_positions"] if eq else 0,
        "as_of":          eq["ts"].isoformat() if eq else None,
    })


@router.get("/portfolio/{portfolio_id}/equity")
async def equity_curve(portfolio_id: int) -> JSONResponse:
    rows = await fetch(
        """
        SELECT ts, equity::float8 AS equity
        FROM equity_snapshots WHERE portfolio_id = $1
        ORDER BY ts
        """,
        portfolio_id,
    )
    return JSONResponse([
        {"ts": r["ts"].isoformat(), "equity": float(r["equity"])} for r in rows
    ])
