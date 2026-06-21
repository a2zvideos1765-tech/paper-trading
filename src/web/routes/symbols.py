"""Universe management — list, search Angel's instrument master, add, remove.

The poller and trader read `universe_symbols` at the start of every cycle, so
add/remove takes effect within ~60s without a restart.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from src.core.config import REPO_ROOT
from src.core.db import execute, fetch, fetchrow
from src.web.auth import require_admin


router = APIRouter()


# ---------- Page ----------

@router.get("/symbols", response_class=HTMLResponse)
async def symbols_page(request: Request) -> HTMLResponse:
    universe = await fetch(
        """
        SELECT u.symbol, u.exchange, u.token, u.kind, u.added_at,
               i.name AS instrument_name, i.instrument_type
        FROM universe_symbols u
        LEFT JOIN instruments i ON i.token = u.token AND i.exchange = u.exchange
        WHERE u.enabled = TRUE
        ORDER BY u.kind, u.symbol
        """,
    )
    instr_meta = await fetchrow(
        "SELECT value, updated_at FROM app_meta WHERE key = 'instruments_refresh'"
    )
    instr_count = await fetchrow("SELECT count(*) AS n FROM instruments")
    pending = await fetch(
        """
        SELECT id, symbol, exchange, interval, state, error,
               enqueued_at, started_at, finished_at
        FROM backfill_queue
        WHERE state IN ('pending', 'running')
           OR finished_at > now() - interval '24 hours'
        ORDER BY enqueued_at DESC
        LIMIT 30
        """,
    )
    return request.app.state.templates.TemplateResponse(
        request, "symbols.html",
        {
            "universe": [dict(r) for r in universe],
            "instr_count": int(instr_count["n"]) if instr_count else 0,
            "instr_meta": (json.loads(instr_meta["value"]) if instr_meta else {}),
            "instr_meta_updated": instr_meta["updated_at"] if instr_meta else None,
            "backfill_recent": [dict(r) for r in pending],
        },
    )


# ---------- Search (typeahead) ----------

@router.get("/api/symbols/search")
async def search_instruments(
    q: str = "",
    limit: int = 25,
    instrument_type: str | None = None,
    exchange: str | None = None,
) -> JSONResponse:
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse([])
    limit = max(1, min(limit, 50))
    q_lower = q.lower()
    args: list = [f"%{q_lower}%", f"{q_lower}%"]
    where = ["(lower(symbol) LIKE $1 OR lower(name) LIKE $1)"]
    if instrument_type:
        args.append(instrument_type.upper())
        where.append(f"instrument_type = ${len(args)}")
    if exchange:
        args.append(exchange.upper())
        where.append(f"exchange = ${len(args)}")
    args.append(limit)
    sql = f"""
        SELECT token, symbol, name, exchange, instrument_type
        FROM instruments
        WHERE {' AND '.join(where)}
        ORDER BY
            CASE WHEN lower(symbol) LIKE $2 THEN 0 ELSE 1 END,
            length(symbol),
            symbol
        LIMIT ${len(args)}
    """
    rows = await fetch(sql, *args)
    return JSONResponse([
        {"token": r["token"], "symbol": r["symbol"], "name": r["name"],
         "exchange": r["exchange"], "instrument_type": r["instrument_type"]}
        for r in rows
    ])


# ---------- Add / remove ----------

class AddSymbolBody(BaseModel):
    token: str
    backfill: bool = True
    kind: Literal["equity", "index"] = "equity"


@router.post("/api/symbols", status_code=201)
async def add_symbol(body: AddSymbolBody, request: Request) -> JSONResponse:
    require_admin(request)
    inst = await fetchrow(
        # A token can now match multiple exchange segments; prefer the cash
        # exchanges (NSE, then BSE) over derivatives/commodities.
        "SELECT token, symbol, exchange, instrument_type FROM instruments WHERE token = $1 "
        "ORDER BY CASE exchange WHEN 'NSE' THEN 0 WHEN 'BSE' THEN 1 ELSE 2 END LIMIT 1",
        body.token,
    )
    if not inst:
        raise HTTPException(404, "Token not found in instrument master. Refresh and try again.")

    await execute(
        """
        INSERT INTO universe_symbols (symbol, exchange, token, kind, enabled)
        VALUES ($1, $2, $3, $4, TRUE)
        ON CONFLICT (symbol, exchange) DO UPDATE
          SET enabled = TRUE, token = EXCLUDED.token, kind = EXCLUDED.kind
        """,
        inst["symbol"], inst["exchange"], inst["token"], body.kind,
    )

    queued = False
    if body.backfill:
        # Equities use 5m, indices use 1d
        interval = "1d" if body.kind == "index" else "5m"
        await execute(
            """
            INSERT INTO backfill_queue (symbol, exchange, token, interval, days)
            VALUES ($1, $2, $3, $4, 200)
            """,
            inst["symbol"], inst["exchange"], inst["token"], interval,
        )
        queued = True

    return JSONResponse({
        "symbol": inst["symbol"], "exchange": inst["exchange"],
        "kind": body.kind, "backfill_queued": queued,
    }, status_code=201)


@router.delete("/api/symbols/{symbol}/{exchange}")
async def remove_symbol(symbol: str, exchange: str, request: Request) -> JSONResponse:
    require_admin(request)
    # Safety: never remove a symbol the Angel account actually holds. The live bot
    # can't exit a symbol that's left the universe (the engine stops loading its
    # candles, so it never generates a SELL), which would strand the real shares.
    # real_holdings stores the broker tradingsymbol (e.g. "AUROPHARMA-EQ"); the
    # universe symbol is bare — match both forms.
    held = await fetchrow(
        "SELECT symbol, qty FROM real_holdings WHERE qty > 0 AND symbol IN ($1, $1 || '-EQ')",
        symbol.upper(),
    )
    if held:
        raise HTTPException(
            409,
            f"{symbol.upper()} is held in your Angel account ({held['qty']} share(s), "
            f"{held['symbol']}). Sell the position first — the bot cannot exit a removed "
            f"symbol — then remove it.",
        )
    result = await execute(
        "UPDATE universe_symbols SET enabled = FALSE WHERE symbol = $1 AND exchange = $2",
        symbol, exchange.upper(),
    )
    # asyncpg returns "UPDATE n"
    n = int(result.split()[-1]) if result and result.startswith("UPDATE") else 0
    if n == 0:
        raise HTTPException(404, "Symbol not in universe.")
    return JSONResponse({"removed": True, "symbol": symbol, "exchange": exchange.upper()})


# ---------- Instrument-master refresh (manual button) ----------

def _spawn_refresh() -> None:
    """Run the refresh tool as a detached subprocess so the request returns fast."""
    venv_python = REPO_ROOT / ".venv" / "bin" / "python"
    python_exe = str(venv_python) if venv_python.exists() else sys.executable
    subprocess.Popen(
        [python_exe, "-m", "tools.refresh_instruments"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@router.post("/api/symbols/refresh", status_code=202)
async def trigger_refresh(background: BackgroundTasks, request: Request) -> JSONResponse:
    require_admin(request)
    # Mark "running" pre-emptively so the UI flips immediately.
    await execute(
        """
        INSERT INTO app_meta (key, value, updated_at)
        VALUES ('instruments_refresh', '{"state": "running"}'::jsonb, now())
        ON CONFLICT (key) DO UPDATE
          SET value = '{"state": "running"}'::jsonb, updated_at = now()
        """,
    )
    background.add_task(_spawn_refresh)
    return JSONResponse({"state": "running"}, status_code=202)


@router.get("/api/symbols/refresh/status")
async def refresh_status() -> JSONResponse:
    row = await fetchrow(
        "SELECT value, updated_at FROM app_meta WHERE key = 'instruments_refresh'"
    )
    count = await fetchrow("SELECT count(*) AS n FROM instruments")
    if not row:
        return JSONResponse({"state": "idle", "count": int(count["n"])})
    val = json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
    return JSONResponse({
        **val,
        "count": int(count["n"]),
        "updated_at": row["updated_at"].isoformat(),
    })


# ---------- Backfill status (for the symbols page) ----------

@router.get("/api/symbols/backfill/status")
async def backfill_status() -> JSONResponse:
    rows = await fetch(
        """
        SELECT id, symbol, exchange, interval, state, error,
               enqueued_at, started_at, finished_at
        FROM backfill_queue
        WHERE state IN ('pending', 'running')
           OR finished_at > now() - interval '24 hours'
        ORDER BY enqueued_at DESC
        LIMIT 50
        """,
    )
    return JSONResponse([
        {
            "id": r["id"],
            "symbol": r["symbol"],
            "exchange": r["exchange"],
            "interval": r["interval"],
            "state": r["state"],
            "error": r["error"],
            "enqueued_at": r["enqueued_at"].isoformat() if r["enqueued_at"] else None,
            "started_at": r["started_at"].isoformat() if r["started_at"] else None,
            "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
        }
        for r in rows
    ])
