"""Download Angel One's instrument master and persist it to the `instruments` table.

Angel publishes the full scrip master at a public URL (no API key required):
    https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json

It's a single JSON array of ~80k objects (~100MB). We download it once, normalize
the fields, and UPSERT into `instruments` in 5,000-row batches.

Run modes:
- Manual:           python -m tools.refresh_instruments
- Weekly (PM2):     paperaglo-instruments cron: "0 3 * * 0" (Sunday 03:00 IST)
- Manual via API:   POST /api/symbols/refresh   (the web app spawns this)
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from typing import Any, Iterable

import httpx

from src.core.db import close_pool, conn, get_pool
from src.core.logging import setup_logging


log = setup_logging("instruments")

URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
BATCH = 5000


def _classify_instrument_type(raw: str, symbol: str, exch_seg: str) -> str:
    """Angel's `instrumenttype` is "" for equities, "FUTSTK"/"FUTIDX"/"OPTSTK"/"OPTIDX"/"AMXIDX"
    for derivatives, etc. We collapse to a small set the dashboard cares about."""
    raw = (raw or "").upper().strip()
    if raw.startswith("FUT"):
        return "FUT"
    if raw.startswith("OPT"):
        return "OPT"
    if "IDX" in raw:
        return "INDEX"
    # Equity: usually empty instrumenttype with -EQ suffix on NSE/BSE
    if symbol.upper().endswith("-EQ"):
        return "EQ"
    return raw or "OTHER"


def _parse_expiry(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    for fmt in ("%d-%b-%Y", "%d%b%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _to_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_decimal(v: Any) -> float | None:
    try:
        f = float(v)
        if f < 0:
            return None
        return f
    except (TypeError, ValueError):
        return None


def _normalize(rows: Iterable[dict]) -> list[tuple]:
    """Map Angel's raw dicts to our `instruments` columns."""
    out: list[tuple] = []
    for r in rows:
        token = str(r.get("token") or "").strip()
        if not token:
            continue
        symbol = str(r.get("symbol") or "").strip()
        if not symbol:
            continue
        name = str(r.get("name") or "").strip() or None
        exch_seg = str(r.get("exch_seg") or "").strip().upper()
        instrument_type = _classify_instrument_type(r.get("instrumenttype", ""), symbol, exch_seg)
        out.append((
            token,
            symbol,
            name,
            exch_seg,                         # exchange
            exch_seg,                         # segment (same for now)
            instrument_type,
            _to_int(r.get("lotsize")),
            _to_decimal(r.get("tick_size")),
            _parse_expiry(r.get("expiry")),
        ))
    return out


async def _bulk_upsert(rows: list[tuple]) -> int:
    if not rows:
        return 0
    inserted = 0
    async with conn() as c:
        for i in range(0, len(rows), BATCH):
            chunk = rows[i : i + BATCH]
            await c.executemany(
                """
                INSERT INTO instruments
                    (token, symbol, name, exchange, segment, instrument_type,
                     lot_size, tick_size, expiry, refreshed_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
                ON CONFLICT (token) DO UPDATE
                  SET symbol          = EXCLUDED.symbol,
                      name            = EXCLUDED.name,
                      exchange        = EXCLUDED.exchange,
                      segment         = EXCLUDED.segment,
                      instrument_type = EXCLUDED.instrument_type,
                      lot_size        = EXCLUDED.lot_size,
                      tick_size       = EXCLUDED.tick_size,
                      expiry          = EXCLUDED.expiry,
                      refreshed_at    = now()
                """,
                chunk,
            )
            inserted += len(chunk)
            log.info("upserted batch", extra={"batch_end": i + len(chunk), "total_so_far": inserted})
    return inserted


async def _record_meta(count: int) -> None:
    payload = {"count": count, "finished_at": datetime.utcnow().isoformat() + "Z"}
    async with conn() as c:
        await c.execute(
            """
            INSERT INTO app_meta (key, value, updated_at)
            VALUES ('instruments_refresh', $1::jsonb, now())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value, updated_at = now()
            """,
            json.dumps(payload),
        )


async def refresh() -> int:
    """Download + UPSERT. Returns row count."""
    log.info("downloading scrip master", extra={"url": URL})
    async with conn() as c:
        await c.execute(
            """
            INSERT INTO app_meta (key, value, updated_at)
            VALUES ('instruments_refresh', '{"state": "running"}'::jsonb, now())
            ON CONFLICT (key) DO UPDATE
              SET value = '{"state": "running"}'::jsonb, updated_at = now()
            """,
        )

    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
        r = await client.get(URL)
        r.raise_for_status()
        raw = r.json()  # ~100MB; fine on a VPS, brief
    log.info("downloaded", extra={"raw_count": len(raw)})

    rows = _normalize(raw)
    log.info("normalized", extra={"normalized_count": len(rows)})
    inserted = await _bulk_upsert(rows)
    await _record_meta(inserted)
    log.info("refresh complete", extra={"inserted": inserted})
    return inserted


async def main() -> None:
    await get_pool()
    try:
        await refresh()
    except Exception as exc:  # noqa: BLE001
        log.exception("refresh failed")
        async with conn() as c:
            await c.execute(
                """
                INSERT INTO app_meta (key, value, updated_at)
                VALUES ('instruments_refresh',
                        $1::jsonb, now())
                ON CONFLICT (key) DO UPDATE
                  SET value = EXCLUDED.value, updated_at = now()
                """,
                json.dumps({"state": "error", "error": str(exc)[:500]}),
            )
        raise
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
