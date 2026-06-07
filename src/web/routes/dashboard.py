"""Home dashboard: grid of all enabled portfolios with key stats."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.core.db import fetch, fetchrow
from src.core.metrics import estimated_apy


router = APIRouter()


async def _portfolio_cards() -> list[dict]:
    rows = await fetch(
        """
        SELECT p.id, p.name, p.strategy_id, p.capital::float8 AS capital,
               p.started_at,
               COALESCE(eq.equity, p.capital)::float8       AS equity,
               COALESCE(eq.cash, p.capital)::float8         AS cash,
               COALESCE(eq.holdings_value, 0)::float8       AS holdings_value,
               COALESCE(eq.open_positions, 0)::int          AS open_positions,
               COALESCE(prev.equity, p.capital)::float8     AS prev_equity,
               (SELECT count(*) FROM trades t WHERE t.portfolio_id = p.id) AS trade_count
        FROM portfolios p
        LEFT JOIN LATERAL (
            SELECT equity, cash, holdings_value, open_positions
            FROM equity_snapshots WHERE portfolio_id = p.id
            ORDER BY ts DESC LIMIT 1
        ) eq ON TRUE
        LEFT JOIN LATERAL (
            SELECT equity FROM equity_snapshots
            WHERE portfolio_id = p.id AND ts < (SELECT max(ts) FROM equity_snapshots WHERE portfolio_id = p.id)
            ORDER BY ts DESC LIMIT 1
        ) prev ON TRUE
        WHERE p.enabled = TRUE
        ORDER BY p.id
        """
    )
    cards = []
    for r in rows:
        equity = float(r["equity"])
        prev = float(r["prev_equity"])
        capital = float(r["capital"])
        day_change = (equity - prev) if prev else 0.0
        day_change_pct = (day_change / prev * 100.0) if prev else 0.0
        total_pct = (equity / capital - 1.0) * 100.0
        cards.append({
            "id": r["id"],
            "name": r["name"],
            "strategy_id": r["strategy_id"],
            "capital": capital,
            "equity": equity,
            "cash": float(r["cash"]),
            "holdings_value": float(r["holdings_value"]),
            "open_positions": int(r["open_positions"]),
            "trade_count": int(r["trade_count"]),
            "day_change": day_change,
            "day_change_pct": day_change_pct,
            "total_pct": total_pct,
            "est_apy_pct": estimated_apy(equity, capital, r["started_at"]),
        })
    return cards


async def _runner_status() -> list[dict]:
    rows = await fetch("SELECT app, last_beat, status, detail FROM runs ORDER BY app")
    out = []
    for r in rows:
        stale = (datetime.now(timezone.utc) - r["last_beat"]) > timedelta(minutes=5)
        out.append({
            "app": r["app"],
            "last_beat": r["last_beat"],
            "status": r["status"],
            "detail": r["detail"],
            "stale": stale,
        })
    return out


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    cards = await _portfolio_cards()
    runners = await _runner_status()
    totals = {
        "equity": sum(c["equity"] for c in cards),
        "capital": sum(c["capital"] for c in cards),
        "day_change": sum(c["day_change"] for c in cards),
    }
    totals["total_pct"] = (totals["equity"] / totals["capital"] - 1) * 100 if totals["capital"] else 0
    return request.app.state.templates.TemplateResponse(
        request, "dashboard.html",
        {"cards": cards, "runners": runners, "totals": totals},
    )
