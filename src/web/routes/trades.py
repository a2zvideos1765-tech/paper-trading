"""Cross-portfolio trade ledger view + CSV exports.

CSV downloads come in two scopes:
  * per portfolio  — GET /portfolio/{id}/trades.csv      (defined in portfolios.py)
  * per strategy   — GET /strategy/{strategy_id}/trades.csv  (merged across that
                     strategy's portfolios, with a Portfolio column to distinguish)

Both reuse `rows_to_csv` / `csv_response` here. Downloads are read-only, so the
auth gate (any logged-in session, incl. viewers) is the only gate — no admin needed.
"""

from __future__ import annotations

import csv
import io
from datetime import timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from src.core.db import fetch
from src.core.time import IST


router = APIRouter()


# ---- shared CSV helpers (imported by portfolios.py too) ----

# Pull every persisted trade column + portfolio/strategy labels, ordered oldest→newest.
TRADES_CSV_SQL = """
    SELECT t.ts, t.symbol, t.side, t.qty,
           t.price::float8 AS price, t.turnover::float8 AS turnover,
           t.charges::float8 AS charges, t.cash_after::float8 AS cash_after, t.reason,
           p.name AS portfolio_name, p.strategy_id
    FROM trades t JOIN portfolios p ON p.id = t.portfolio_id
    WHERE {where}
    ORDER BY t.ts, p.name, t.symbol
"""

_CSV_HEADER = [
    "When (IST)", "Portfolio", "Strategy", "Symbol", "Side", "Qty",
    "Price", "Turnover", "Charges", "Cash After", "Reason",
]


def rows_to_csv(rows) -> str:
    """Render trade rows as CSV. Timestamps are converted UTC→IST (asyncpg returns
    timezone-aware UTC) so the exported 'When' matches the dashboard."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_CSV_HEADER)
    for r in rows:
        ts = r["ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_ist = ts.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
        w.writerow([
            ts_ist,
            r["portfolio_name"],
            r["strategy_id"],
            r["symbol"],
            r["side"],
            r["qty"],
            f'{r["price"]:.2f}',
            f'{r["turnover"]:.2f}',
            f'{r["charges"]:.2f}',
            f'{r["cash_after"]:.2f}',
            r["reason"],
        ])
    return buf.getvalue()


def csv_response(content: str, filename: str) -> Response:
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---- routes ----

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
    # Per-strategy export menu: one entry per strategy that has trades.
    strategies = await fetch(
        """
        SELECT p.strategy_id,
               count(*)                         AS trade_count,
               count(DISTINCT p.id)             AS portfolio_count
        FROM trades t JOIN portfolios p ON p.id = t.portfolio_id
        GROUP BY p.strategy_id
        ORDER BY p.strategy_id
        """
    )
    return request.app.state.templates.TemplateResponse(
        request, "trades.html",
        {
            "trades": [dict(r) for r in rows],
            "limit": limit,
            "strategies": [dict(s) for s in strategies],
        },
    )


@router.get("/strategy/{strategy_id}/trades.csv")
async def strategy_trades_csv(strategy_id: str) -> Response:
    """All trades across every portfolio running `strategy_id`, merged into one CSV."""
    rows = await fetch(TRADES_CSV_SQL.format(where="p.strategy_id = $1"), strategy_id)
    return csv_response(rows_to_csv(rows), f"{strategy_id}_trades.csv")
