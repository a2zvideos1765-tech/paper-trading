"""Operational alert feed for the header notification bell + a downloadable
debug bundle.

  GET /api/alerts?hours=24          — JSON feed (summary + newest-first alerts)
  GET /api/alerts/download?hours=48 — Markdown debug bundle (attachment) to paste
                                       into Claude for a diagnosis

Both are admin-only: the feed surfaces log lines that can carry order detail and
broker error text (same posture as /api/bot/logs).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from src.core.alerts import build_debug_bundle, collect_alerts
from src.core.time import now_ist
from src.web.auth import require_admin


router = APIRouter()


@router.get("/api/alerts")
async def api_alerts(request: Request, hours: int = 24) -> JSONResponse:
    require_admin(request)
    data = await collect_alerts(hours)
    return JSONResponse(data)


@router.get("/api/alerts/download")
async def api_alerts_download(request: Request, hours: int = 48) -> PlainTextResponse:
    require_admin(request)
    data = await collect_alerts(hours)
    text = build_debug_bundle(data)
    fname = f"paper-trading-debug-{now_ist():%Y%m%d-%H%M}.md"
    return PlainTextResponse(
        text,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
