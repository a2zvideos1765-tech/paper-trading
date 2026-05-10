"""Cross-portfolio trade ledger view."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.core.db import fetch


router = APIRouter()


@router.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request, limit: int = 200) -> HTMLResponse:
    limit = max(1, min(limit, 1000))
    rows = await fetch(
        """
        SELECT t.ts, t.symbol, t.side, t.qty,
               t.price::float8, t.charges::float8, t.cash_after::float8, t.reason,
               p.name AS portfolio_name, p.id AS portfolio_id
        FROM trades t JOIN portfolios p ON p.id = t.portfolio_id
        ORDER BY t.ts DESC LIMIT $1
        """,
        limit,
    )
    return request.app.state.templates.TemplateResponse(
        request, "trades.html", {"trades": [dict(r) for r in rows], "limit": limit},
    )
