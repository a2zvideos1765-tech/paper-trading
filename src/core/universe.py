"""Symbol universe loader.

The user-facing source of truth is the `universe_symbols` table — edited from
the dashboard's /symbols page. The poller and trader read this every cycle,
so add/remove takes effect within ~60s.

For first-run bootstrap (fresh DB, no rows yet) we copy `config/universe.yaml`
in. After that the YAML is purely historical — edits there have no effect
unless the DB table is wiped.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from src.core.config import REPO_ROOT
from src.core.db import conn, fetch


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    token: str
    exchange: str


# ---------- YAML bootstrap (fallback only, on empty DB) ----------

def _load_yaml() -> tuple[list[SymbolSpec], list[SymbolSpec]]:
    path = REPO_ROOT / "config" / "universe.yaml"
    if not path.exists():
        return [], []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    eq = [SymbolSpec(**row) for row in (data.get("symbols") or [])]
    idx = [SymbolSpec(**row) for row in (data.get("indices") or [])]
    return eq, idx


async def _bootstrap_db(equities: list[SymbolSpec], indices: list[SymbolSpec]) -> None:
    rows = (
        [(s.symbol, s.exchange, s.token, "equity") for s in equities]
        + [(s.symbol, s.exchange, s.token, "index") for s in indices]
    )
    if not rows:
        return
    async with conn() as c:
        await c.executemany(
            """
            INSERT INTO universe_symbols (symbol, exchange, token, kind, enabled)
            VALUES ($1, $2, $3, $4, TRUE)
            ON CONFLICT (symbol, exchange) DO NOTHING
            """,
            rows,
        )


# ---------- Public API (async) ----------

async def load_universe() -> tuple[list[SymbolSpec], list[SymbolSpec]]:
    """Returns (equities, indices). DB-first; bootstraps from YAML on empty."""
    rows = await fetch(
        "SELECT symbol, exchange, token, kind FROM universe_symbols WHERE enabled = TRUE"
    )
    if not rows:
        eq, idx = _load_yaml()
        await _bootstrap_db(eq, idx)
        return eq, idx
    eq: list[SymbolSpec] = []
    idx: list[SymbolSpec] = []
    for r in rows:
        spec = SymbolSpec(symbol=r["symbol"], token=r["token"], exchange=r["exchange"])
        (idx if r["kind"] == "index" else eq).append(spec)
    return eq, idx


async def all_specs() -> list[SymbolSpec]:
    eq, idx = await load_universe()
    return [*eq, *idx]


# ---------- Sync wrapper (for the rare caller that can't go async) ----------

def load_universe_sync() -> tuple[list[SymbolSpec], list[SymbolSpec]]:
    """Synchronous YAML-only loader. Only used by tools/tests where the
    asyncpg pool isn't worth spinning up. Production paths use load_universe()."""
    return _load_yaml()
