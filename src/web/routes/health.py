"""Health endpoint for monitoring + dashboard top-bar indicator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.core.db import fetch, fetchrow


router = APIRouter()


@router.get("/health")
async def health() -> JSONResponse:
    payload: dict = {"status": "ok", "checks": {}}

    # Database round-trip + heartbeats
    try:
        runs = await fetch("SELECT app, last_beat, status, detail FROM runs ORDER BY app")
        payload["checks"]["db"] = "ok"
        payload["runs"] = [
            {
                "app": r["app"],
                "last_beat": r["last_beat"].isoformat(),
                "status": r["status"],
                "detail": r["detail"],
                "stale": (datetime.now(timezone.utc) - r["last_beat"]) > timedelta(minutes=5),
            }
            for r in runs
        ]
    except Exception as exc:  # noqa: BLE001
        payload["status"] = "error"
        payload["checks"]["db"] = f"error: {exc}"

    # Last candle ts (sanity check for the poller)
    try:
        last = await fetchrow("SELECT max(ts) AS t FROM candles WHERE interval = '1m'")
        payload["last_candle_ts"] = last["t"].isoformat() if last and last["t"] else None
    except Exception:  # noqa: BLE001
        payload["last_candle_ts"] = None

    code = 200 if payload["status"] == "ok" else 503
    return JSONResponse(payload, status_code=code)
