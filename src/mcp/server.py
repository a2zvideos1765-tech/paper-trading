"""MCP server for the paperaglo real-money trading platform.

Exposes read + minor-write tools so Claude can inspect and manage the platform
without any GUI access. Runs as a standalone HTTP/SSE process (paperaglo-mcp),
reverse-proxied by Caddy behind a bearer token.

Transport: streamable-http (preferred for Claude Desktop / Claude.ai)
Auth:      Authorization: Bearer <MCP_TOKEN>   (checked per-request in middleware)
Port:      settings.mcp_port (default 8001, override with MCP_PORT env var)

Read tools (no side effects):
  get_health          — runner heartbeats + market clock
  list_portfolios     — all portfolios (paper + live)
  get_trades          — recent trades for a portfolio or strategy
  get_positions       — current open positions
  get_equity          — latest equity snapshot
  get_bot_status      — real_bot_state + real-trader runner beat
  get_funds           — latest Angel fund snapshot
  get_holdings        — Angel holdings mirror
  get_real_orders     — real-order ledger
  tail_logs           — last N lines of a runner's log file
  run_sql             — read-only SELECT (rejects INSERT/UPDATE/DROP …)

Minor-write tools (admin-equivalent; no order placement):
  add_symbol          — add a symbol to universe_symbols
  remove_symbol       — disable a symbol from universe_symbols
  toggle_bot          — flip real_bot_state.enabled
  set_param           — write one field to portfolio_overrides (validated)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.core.config import REPO_ROOT, settings
from src.core.db import close_pool, fetch, fetchrow, get_pool
from src.core.logging import setup_logging
from src.core.time import IST, is_market_open, now_ist
from src.strategies.registry import get as get_strategy
from src.strategies.validation import coerce_and_apply


log = setup_logging("mcp")

# ---------- FastMCP instance ----------
# The name "paperaglo" is shown in Claude's tool list.
mcp = FastMCP(
    "paperaglo",
    instructions=(
        "Tools for the paperaglo paper-trading + live-trading platform. "
        "Use get_health first to confirm the platform is running. "
        "Never place orders via MCP — use toggle_bot to enable the real bot. "
        "set_param validates the value against the strategy schema before writing."
    ),
)


# ---------- Internal helpers ----------

def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).isoformat()


def _row(r) -> dict:
    """Convert asyncpg Record → plain dict, serialising datetimes to ISO IST."""
    out: dict[str, Any] = {}
    for k, v in r.items():
        if isinstance(v, datetime):
            out[k] = _iso(v)
        else:
            out[k] = v
    return out


def _rows(rs) -> list[dict]:
    return [_row(r) for r in rs]


# ---------- READ TOOLS ----------

@mcp.tool()
async def get_health() -> dict:
    """Return the heartbeat status of all platform runners and the market clock."""
    beats = await fetch("SELECT app, last_beat, status, detail FROM runs ORDER BY app")
    return {
        "market_open": is_market_open(),
        "now_ist": now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        "runners": _rows(beats),
    }


@mcp.tool()
async def list_portfolios() -> list[dict]:
    """List all portfolios (paper + live) with their current equity snapshot."""
    rows = await fetch(
        """
        SELECT p.id, p.name, p.strategy_id, p.capital::float8, p.enabled, p.live, p.started_at,
               e.equity::float8 AS equity, e.cash::float8 AS cash,
               e.holdings_value::float8 AS holdings_value
        FROM portfolios p
        LEFT JOIN LATERAL (
            SELECT equity, cash, holdings_value
            FROM equity_snapshots WHERE portfolio_id = p.id ORDER BY ts DESC LIMIT 1
        ) e ON TRUE
        ORDER BY p.live DESC, p.id
        """
    )
    return _rows(rows)


@mcp.tool()
async def get_trades(
    portfolio_id: int | None = None,
    strategy_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return recent trades. Filter by portfolio_id or strategy_id (or both)."""
    limit = max(1, min(limit, 500))
    where_parts = []
    args: list[Any] = []
    if portfolio_id is not None:
        args.append(portfolio_id)
        where_parts.append(f"t.portfolio_id = ${len(args)}")
    if strategy_id is not None:
        args.append(strategy_id)
        where_parts.append(f"p.strategy_id = ${len(args)}")
    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    args.append(limit)
    rows = await fetch(
        f"""
        SELECT t.id, t.portfolio_id, p.name AS portfolio_name, p.strategy_id,
               t.symbol, t.side, t.qty, t.price::float8, t.date, t.time, t.reason
        FROM trades t
        JOIN portfolios p ON p.id = t.portfolio_id
        {where}
        ORDER BY t.date DESC, t.time DESC
        LIMIT ${len(args)}
        """,
        *args,
    )
    return _rows(rows)


@mcp.tool()
async def get_positions(portfolio_id: int | None = None) -> list[dict]:
    """Return current open positions, optionally filtered by portfolio."""
    where = f"WHERE pos.portfolio_id = $1" if portfolio_id is not None else ""
    args = [portfolio_id] if portfolio_id is not None else []
    rows = await fetch(
        f"""
        SELECT pos.portfolio_id, p.name AS portfolio_name, p.strategy_id,
               pos.symbol, pos.qty, pos.avg_price::float8, pos.entry_price::float8,
               pos.entry_date, pos.peak_price::float8, pos.tiers_hit, pos.pyramid_adds_hit
        FROM positions pos
        JOIN portfolios p ON p.id = pos.portfolio_id
        {where}
        ORDER BY p.id, pos.symbol
        """,
        *args,
    )
    return _rows(rows)


@mcp.tool()
async def get_equity(portfolio_id: int | None = None) -> list[dict]:
    """Return the latest equity snapshot for each portfolio (or one if filtered)."""
    if portfolio_id is not None:
        rows = await fetch(
            """
            SELECT portfolio_id, cash::float8, holdings_value::float8,
                   equity::float8, open_positions, ts
            FROM equity_snapshots WHERE portfolio_id = $1 ORDER BY ts DESC LIMIT 1
            """,
            portfolio_id,
        )
    else:
        rows = await fetch(
            """
            SELECT DISTINCT ON (portfolio_id) portfolio_id,
                   cash::float8, holdings_value::float8, equity::float8, open_positions, ts
            FROM equity_snapshots ORDER BY portfolio_id, ts DESC
            """
        )
    return _rows(rows)


@mcp.tool()
async def get_bot_status() -> dict:
    """Return the live-bot master switch state + real-trader runner heartbeat."""
    bot = await fetchrow("SELECT enabled, note, updated_at, updated_by FROM real_bot_state WHERE id = 1")
    beat = await fetchrow("SELECT last_beat, status, detail FROM runs WHERE app = 'real_trader'")
    funds = await fetchrow(
        "SELECT available_cash::float8, net::float8, utilised::float8, as_of "
        "FROM real_funds ORDER BY as_of DESC LIMIT 1"
    )
    return {
        "bot_enabled": bool(bot["enabled"]) if bot else False,
        "note": bot["note"] if bot else None,
        "updated_at": _iso(bot["updated_at"]) if bot else None,
        "updated_by": bot["updated_by"] if bot else None,
        "market_open": is_market_open(),
        "runner_status": beat["status"] if beat else None,
        "runner_detail": beat["detail"] if beat else None,
        "last_beat": _iso(beat["last_beat"]) if beat else None,
        "funds": {
            "available_cash": float(funds["available_cash"]) if funds else None,
            "net": float(funds["net"]) if funds and funds["net"] is not None else None,
            "as_of": _iso(funds["as_of"]) if funds else None,
        },
    }


@mcp.tool()
async def get_funds() -> dict:
    """Return the latest Angel One fund snapshot (available cash, net, utilised)."""
    row = await fetchrow(
        "SELECT available_cash::float8, net::float8, utilised::float8, as_of "
        "FROM real_funds ORDER BY as_of DESC LIMIT 1"
    )
    if not row:
        return {"available_cash": None, "net": None, "utilised": None, "as_of": None,
                "note": "No funds snapshot yet — real_trader may not have run."}
    return {
        "available_cash": float(row["available_cash"]),
        "net": float(row["net"]) if row["net"] is not None else None,
        "utilised": float(row["utilised"]) if row["utilised"] is not None else None,
        "as_of": _iso(row["as_of"]),
    }


@mcp.tool()
async def get_holdings() -> list[dict]:
    """Return the Angel One holdings mirror (last sync from broker)."""
    rows = await fetch(
        "SELECT symbol, qty, avg_price::float8, ltp::float8, pnl::float8, as_of "
        "FROM real_holdings ORDER BY symbol"
    )
    return _rows(rows)


@mcp.tool()
async def get_real_orders(limit: int = 50) -> list[dict]:
    """Return recent real orders with their Angel broker status and fill details."""
    limit = max(1, min(limit, 200))
    rows = await fetch(
        """
        SELECT id, portfolio_id, intent_key, symbol, side, qty,
               order_type, product, requested_price::float8, angel_order_id,
               status, filled_qty, avg_fill_price::float8, reason, error,
               requested_at, updated_at
        FROM real_orders ORDER BY requested_at DESC LIMIT $1
        """,
        limit,
    )
    return _rows(rows)


@mcp.tool()
async def tail_logs(app: str = "real_trader", lines: int = 50) -> dict:
    """Return the last N lines of today's log file for the given app name.

    Valid app names: real_trader, trader, poller, web, backfill, mcp.
    Logs are JSON-per-line; returned as a list of parsed objects.
    """
    lines = max(1, min(lines, 500))
    # Lowercase first, then strip anything path-like — blocks traversal while
    # keeping "Real_Trader" → "real_trader" instead of mangling it.
    safe = re.sub(r"[^a-z0-9_\-]", "", app.lower())
    if not safe:
        return {"error": "invalid app name"}

    from src.core.time import now_ist as _now_ist
    today = _now_ist().strftime("%Y-%m-%d")
    log_path = settings.log_dir / today / f"{safe}.log"

    if not log_path.exists():
        # Try yesterday as fallback (useful for midnight boundary)
        from datetime import timedelta
        yesterday = (_now_ist() - timedelta(days=1)).strftime("%Y-%m-%d")
        log_path = settings.log_dir / yesterday / f"{safe}.log"
        if not log_path.exists():
            return {"error": f"log not found: logs/{today}/{safe}.log", "app": safe, "lines_requested": lines}

    text = log_path.read_text(encoding="utf-8", errors="replace")
    all_lines = text.splitlines()
    tail = all_lines[-lines:]
    parsed = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append({"raw": line})

    return {"app": safe, "log_path": str(log_path), "total_lines": len(all_lines),
            "tail_lines": len(parsed), "entries": parsed}


# `into` is blocked because `SELECT … INTO new_table` is DDL in Postgres.
_BLOCKED_SQL = re.compile(
    r"\b(insert|update|delete|drop|truncate|create|alter|grant|revoke|copy|vacuum|analyze|into|do|call|set|listen|notify)\b",
    re.IGNORECASE,
)
# Server-side functions that can DoS, read files, or kill other sessions.
_BLOCKED_FUNCS = re.compile(
    r"\b(pg_sleep|pg_terminate_backend|pg_cancel_backend|pg_read_file|pg_read_binary_file|pg_ls_dir|lo_import|lo_export|dblink|set_config)\b",
    re.IGNORECASE,
)

@mcp.tool()
async def run_sql(query: str, limit: int = 100) -> dict:
    """Run a read-only SELECT query against the platform DB and return results.

    Rejects anything that contains DML/DDL keywords (INSERT, UPDATE, DELETE, DROP,
    INTO …) or dangerous server-side functions. Results are capped at `limit` rows
    (max 500). Use this for ad-hoc inspection only.
    """
    stripped = query.strip()
    if not stripped.upper().startswith("SELECT"):
        return {"error": "Only SELECT queries are allowed."}
    if _BLOCKED_SQL.search(stripped):
        return {"error": "Query contains a blocked keyword (INSERT/UPDATE/DELETE/DROP/INTO etc.)."}
    if _BLOCKED_FUNCS.search(stripped):
        return {"error": "Query contains a blocked server-side function."}
    limit = max(1, min(limit, 500))

    # Append LIMIT if the query doesn't already have one, to prevent huge result sets.
    if not re.search(r"\blimit\b", stripped, re.IGNORECASE):
        stripped = f"{stripped} LIMIT {limit}"

    try:
        rows = await fetch(stripped)
        return {"rows": _rows(rows), "count": len(rows)}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# ---------- MINOR-WRITE TOOLS ----------

@mcp.tool()
async def add_symbol(
    token: str,
    kind: str = "equity",
    backfill: bool = True,
) -> dict:
    """Add a symbol to the shared universe watch list by Angel token.

    `kind` must be 'equity' or 'index'. `backfill` queues a 200-day historical
    data pull. Takes effect within ~60s (the next poller/trader cycle).
    """
    if kind not in {"equity", "index"}:
        return {"error": "kind must be 'equity' or 'index'"}

    inst = await fetchrow(
        "SELECT token, symbol, exchange, instrument_type FROM instruments WHERE token = $1",
        token,
    )
    if not inst:
        return {"error": f"Token {token!r} not found in instrument master. Run refresh first."}

    from src.core.db import execute
    await execute(
        """
        INSERT INTO universe_symbols (symbol, exchange, token, kind, enabled)
        VALUES ($1, $2, $3, $4, TRUE)
        ON CONFLICT (symbol, exchange) DO UPDATE
          SET enabled = TRUE, token = EXCLUDED.token, kind = EXCLUDED.kind
        """,
        inst["symbol"], inst["exchange"], inst["token"], kind,
    )

    queued = False
    if backfill:
        interval = "1d" if kind == "index" else "5m"
        await execute(
            "INSERT INTO backfill_queue (symbol, exchange, token, interval, days) VALUES ($1,$2,$3,$4,200)",
            inst["symbol"], inst["exchange"], inst["token"], interval,
        )
        queued = True

    log.info("mcp: add_symbol", extra={"symbol": inst["symbol"], "exchange": inst["exchange"], "kind": kind})
    return {"added": True, "symbol": inst["symbol"], "exchange": inst["exchange"],
            "kind": kind, "backfill_queued": queued}


@mcp.tool()
async def remove_symbol(symbol: str, exchange: str) -> dict:
    """Disable a symbol from the shared universe watch list.

    The symbol's historical candles are kept; only the enabled flag is cleared.
    Takes effect within ~60s.
    """
    from src.core.db import execute
    result = await execute(
        "UPDATE universe_symbols SET enabled = FALSE WHERE symbol = $1 AND exchange = $2",
        symbol.upper(), exchange.upper(),
    )
    n = int(result.split()[-1]) if result and result.startswith("UPDATE") else 0
    if n == 0:
        return {"error": f"{symbol.upper()} / {exchange.upper()} not found in universe."}
    log.info("mcp: remove_symbol", extra={"symbol": symbol, "exchange": exchange})
    return {"removed": True, "symbol": symbol.upper(), "exchange": exchange.upper()}


@mcp.tool()
async def toggle_bot(enabled: bool) -> dict:
    """Flip the live-trading master kill switch.

    When enabled=True the real_trader will start placing CNC LIMIT orders at the
    engine's decided price on the next market-hours tick. When False it switches
    to shadow-sync-only mode (reads funds/holdings but places nothing).

    This is the only safe way to start/stop real trading without restarting the runner.
    """
    from src.core.db import execute
    # Upsert so the toggle works even if sql/007's seed row is missing.
    await execute(
        """
        INSERT INTO real_bot_state (id, enabled, updated_at, updated_by)
        VALUES (1, $1, now(), 'mcp')
        ON CONFLICT (id) DO UPDATE
          SET enabled = $1, updated_at = now(), updated_by = 'mcp'
        """,
        enabled,
    )
    log.info("mcp: toggle_bot", extra={"enabled": enabled})
    return {
        "bot_enabled": enabled,
        "market_open": is_market_open(),
        "now_ist": now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
        "note": "Orders will be placed on the next tick." if enabled else "Bot is now OFF — no orders will be placed.",
    }


@mcp.tool()
async def set_param(portfolio_name: str, field: str, value: Any) -> dict:
    """Write a single strategy parameter override for a portfolio.

    The value is validated through the strategy schema (coerce_and_apply) before
    being written. Returns the resulting override set on success, or a field-level
    error if validation fails.

    Examples:
      set_param("S404_live_sip_20k", "min_entry_cash", 5000)
      set_param("S404_live_sip_20k", "max_positions", 6)
    """
    p = await fetchrow(
        "SELECT id, strategy_id FROM portfolios WHERE name = $1", portfolio_name
    )
    if not p:
        return {"error": f"Portfolio {portfolio_name!r} not found."}

    try:
        base_strategy = get_strategy(p["strategy_id"])
    except KeyError as e:
        return {"error": str(e)}

    # Load existing overrides (if any) and merge the new field in.
    existing_row = await fetchrow(
        "SELECT overrides FROM portfolio_overrides WHERE portfolio_id = $1", p["id"]
    )
    existing: dict[str, Any] = {}
    if existing_row:
        ov = existing_row["overrides"]
        if isinstance(ov, str):
            existing = json.loads(ov)
        elif isinstance(ov, dict):
            existing = ov

    new_overrides = {**existing, field: value}
    _new_strategy, errs = coerce_and_apply(base_strategy, new_overrides)
    if errs:
        return {"error": errs}

    from src.core.db import execute
    await execute(
        """
        INSERT INTO portfolio_overrides (portfolio_id, overrides, updated_at)
        VALUES ($1, $2::jsonb, now())
        ON CONFLICT (portfolio_id) DO UPDATE
          SET overrides = $2::jsonb, updated_at = now()
        """,
        p["id"], json.dumps(new_overrides),
    )
    log.info("mcp: set_param", extra={
        "portfolio": portfolio_name, "field": field, "value": value,
    })
    return {"updated": True, "portfolio": portfolio_name, "overrides": new_overrides}


# ---------- Auth middleware ----------

class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests that don't carry the correct bearer token.

    Skipped entirely when settings.mcp_token is None (so developers can test
    locally without a token). On the VPS, set MCP_TOKEN in .env.
    """

    async def dispatch(self, request: Request, call_next):
        token = settings.mcp_token
        if token:
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return Response("Unauthorized — Bearer token required.", status_code=401,
                                media_type="text/plain")
            candidate = auth_header[len("Bearer "):]
            # Constant-time compare to avoid timing attacks.
            if not hmac.compare_digest(candidate.encode(), token.encode()):
                return Response("Unauthorized — invalid token.", status_code=403,
                                media_type="text/plain")
        return await call_next(request)


# ---------- Lifespan (DB pool + MCP session manager) ----------

@asynccontextmanager
async def _lifespan(_app):
    """Outer lifespan for the mounted MCP app.

    Starlette does NOT run the lifespan of mounted sub-apps, and the
    streamable-http transport requires its StreamableHTTPSessionManager task
    group to be running (otherwise every request 500s with "Task group is not
    initialized"). So we run it here, alongside the DB pool — this is the
    documented pattern for mounting FastMCP inside an existing ASGI server.
    """
    await get_pool()
    log.info("mcp server started",
             extra={"host": settings.mcp_host, "port": settings.mcp_port,
                    "auth": "token set" if settings.mcp_token else "NO TOKEN (insecure)"})
    manager = getattr(mcp, "session_manager", None)
    if manager is not None:
        async with manager.run():
            yield
    else:  # very old SDK (SSE transport only) — no session manager to run
        yield
    await close_pool()


# ---------- Build the ASGI app ----------

def build_app():
    """Return the Starlette ASGI app with auth middleware injected.

    Prefers the streamable-http transport (mcp >= 1.9, what Claude connects to
    at /mcp); falls back to the legacy SSE transport on older SDKs.
    """
    try:
        inner = mcp.streamable_http_app()  # serves at /mcp
    except AttributeError:
        inner = mcp.sse_app()              # legacy transport, serves at /sse

    from starlette.applications import Starlette
    from starlette.routing import Mount

    app = Starlette(lifespan=_lifespan, routes=[Mount("/", app=inner)])
    app.add_middleware(_BearerAuthMiddleware)
    return app


# Build it at module level so `uvicorn src.mcp.server:app` works.
app = build_app()


# ---------- Entry point ----------

if __name__ == "__main__":
    uvicorn.run(
        "src.mcp.server:app",
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
        # access_log=False: JSON structured logging is handled by setup_logging()
        access_log=False,
    )
