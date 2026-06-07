"""JSON API for HTMX polling, the equity curve chart, and the strategy parameter editor."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.core.db import execute, fetch, fetchrow
from src.core.time import IST
from src.strategies.registry import get as get_strategy
from src.strategies.schema import field_defaults, public_schema
from src.strategies.validation import coerce_and_apply
from src.web.auth import require_admin


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
    # Prefer the live intraday point (minute resolution, true clock time); fall back
    # to the daily snapshot before the first tick of the session.
    eq = await fetchrow(
        """
        SELECT cash::float8, holdings_value::float8, equity::float8, open_positions, ts
        FROM equity_intraday WHERE portfolio_id = $1 ORDER BY ts DESC LIMIT 1
        """,
        portfolio_id,
    )
    if eq is None:
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
        # IST so the dashboard shows a real local clock time, not UTC.
        "as_of":          eq["ts"].astimezone(IST).isoformat() if eq and eq["ts"] else None,
    })


@router.get("/portfolio/{portfolio_id}/equity")
async def equity_curve(portfolio_id: int) -> JSONResponse:
    """Equity curve + capital-normalized NIFTY/SENSEX benchmarks for the same window.

    Both benchmarks start at the portfolio's capital so the chart reads as
    "what would buying-and-holding the index from day one have earned".
    """
    p = await fetchrow(
        "SELECT id, capital::float8 AS capital FROM portfolios WHERE id = $1",
        portfolio_id,
    )
    if not p:
        raise HTTPException(404)
    capital = float(p["capital"])

    rows = await fetch(
        """
        WITH es AS (
            SELECT ts, equity::float8 AS equity
              FROM equity_snapshots
             WHERE portfolio_id = $1
             ORDER BY ts
        ),
        first_ts AS (SELECT (SELECT min(ts) FROM es) AS t),
        nifty_base AS (
            SELECT close::float8 AS c FROM candles, first_ts ft
             WHERE symbol = 'NIFTY_50' AND interval = '1d' AND ts::date <= ft.t::date
             ORDER BY ts DESC LIMIT 1
        ),
        sensex_base AS (
            SELECT close::float8 AS c FROM candles, first_ts ft
             WHERE symbol = 'SENSEX' AND interval = '1d' AND ts::date <= ft.t::date
             ORDER BY ts DESC LIMIT 1
        )
        SELECT es.ts,
               es.equity,
               n.close::float8 AS nifty_close,
               s.close::float8 AS sensex_close,
               (SELECT c FROM nifty_base)  AS nifty_base,
               (SELECT c FROM sensex_base) AS sensex_base
          FROM es
          LEFT JOIN candles n ON n.symbol = 'NIFTY_50' AND n.interval = '1d' AND n.ts::date = es.ts::date
          LEFT JOIN candles s ON s.symbol = 'SENSEX'   AND s.interval = '1d' AND s.ts::date = es.ts::date
         ORDER BY es.ts
        """,
        portfolio_id,
    )

    out = []
    for r in rows:
        nifty_norm = None
        sensex_norm = None
        if r["nifty_close"] is not None and r["nifty_base"]:
            nifty_norm = float(r["nifty_close"]) / float(r["nifty_base"]) * capital
        if r["sensex_close"] is not None and r["sensex_base"]:
            sensex_norm = float(r["sensex_close"]) / float(r["sensex_base"]) * capital
        out.append({
            "ts": r["ts"].astimezone(IST).isoformat(),
            "equity": float(r["equity"]),
            "nifty": nifty_norm,
            "sensex": sensex_norm,
        })

    # Append today's intraday points so the curve moves during the live session.
    # Benchmarks are daily, so they stay None for the intraday tail.
    intraday = await fetch(
        """
        SELECT ts, equity::float8 AS equity
        FROM equity_intraday
        WHERE portfolio_id = $1 AND ts::date = (now() AT TIME ZONE 'Asia/Kolkata')::date
        ORDER BY ts
        """,
        portfolio_id,
    )
    for r in intraday:
        out.append({
            "ts": r["ts"].astimezone(IST).isoformat(),
            "equity": float(r["equity"]),
            "nifty": None,
            "sensex": None,
        })
    return JSONResponse(out)


# ---------- Strategy parameter editor ----------

def _tuples_to_lists(v: Any) -> Any:
    if isinstance(v, tuple):
        return [_tuples_to_lists(x) for x in v]
    if isinstance(v, list):
        return [_tuples_to_lists(x) for x in v]
    return v


def _strategy_to_dict(s: Any) -> dict[str, Any]:
    """Render a StrategyV2 instance as JSON-friendly field values."""
    out: dict[str, Any] = {}
    for fname in field_defaults().keys():
        out[fname] = _tuples_to_lists(getattr(s, fname))
    return out


@router.get("/portfolio/{portfolio_id}/params")
async def get_params(portfolio_id: int) -> JSONResponse:
    p = await fetchrow(
        "SELECT id, name, strategy_id, capital::float8 FROM portfolios WHERE id = $1",
        portfolio_id,
    )
    if not p:
        raise HTTPException(404, "Portfolio not found")
    base = get_strategy(p["strategy_id"])
    row = await fetchrow(
        "SELECT overrides, updated_at FROM portfolio_overrides WHERE portfolio_id = $1",
        portfolio_id,
    )
    overrides_raw: dict[str, Any] = {}
    updated_at = None
    if row:
        v = row["overrides"]
        overrides_raw = v if isinstance(v, dict) else (json.loads(v) if v else {})
        updated_at = row["updated_at"].isoformat() if row["updated_at"] else None

    effective, errs = coerce_and_apply(base, overrides_raw)
    return JSONResponse({
        "portfolio": {"id": p["id"], "name": p["name"],
                      "strategy_id": p["strategy_id"], "capital": p["capital"]},
        "schema": public_schema(),
        "defaults": _strategy_to_dict(base),
        "overrides": overrides_raw,
        "effective": _strategy_to_dict(effective),
        "errors": errs,
        "updated_at": updated_at,
    })


class PutParamsBody(BaseModel):
    overrides: dict[str, Any]


@router.put("/portfolio/{portfolio_id}/params")
async def put_params(portfolio_id: int, body: PutParamsBody, request: Request) -> JSONResponse:
    require_admin(request)
    p = await fetchrow(
        "SELECT id, strategy_id FROM portfolios WHERE id = $1",
        portfolio_id,
    )
    if not p:
        raise HTTPException(404, "Portfolio not found")
    base = get_strategy(p["strategy_id"])
    # Drop fields whose value matches the strategy default — keeps the JSONB tidy.
    defaults = _strategy_to_dict(base)
    pruned: dict[str, Any] = {}
    for k, v in body.overrides.items():
        if k in defaults and _tuples_to_lists(defaults[k]) == _tuples_to_lists(v):
            continue
        pruned[k] = v

    effective, errs = coerce_and_apply(base, pruned)
    if errs:
        return JSONResponse({"errors": errs}, status_code=400)

    await execute(
        """
        INSERT INTO portfolio_overrides (portfolio_id, overrides, updated_at)
        VALUES ($1, $2::jsonb, now())
        ON CONFLICT (portfolio_id) DO UPDATE
          SET overrides = EXCLUDED.overrides, updated_at = now()
        """,
        portfolio_id, json.dumps(pruned),
    )
    return JSONResponse({
        "saved": True,
        "overrides": pruned,
        "effective": _strategy_to_dict(effective),
    })


@router.delete("/portfolio/{portfolio_id}/params")
async def delete_params(portfolio_id: int, request: Request) -> JSONResponse:
    require_admin(request)
    await execute(
        "DELETE FROM portfolio_overrides WHERE portfolio_id = $1",
        portfolio_id,
    )
    return JSONResponse({"reset": True})
