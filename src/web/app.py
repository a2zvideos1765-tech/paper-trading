"""FastAPI app — Kite-like paper-trading dashboard.

Routes are split across src/web/routes/. This file wires the app together.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from src.core.config import REPO_ROOT, settings
from src.core.db import close_pool, get_pool
from src.core.logging import setup_logging
from src.core.time import IST
from src.web.auth import is_authenticated, is_viewer
from src.web.routes import api, bot, dashboard, diagnose, health, login, portfolios, symbols, trades


log = setup_logging("web")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    log.info("web app started")
    yield
    await close_pool()


app = FastAPI(lifespan=lifespan, title="Paper Trading", docs_url=None, redoc_url=None)

# Jinja templates exposed on app.state so routes share one renderer.
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR    = Path(__file__).parent / "static"


def _is_viewer_ctx(request: Request) -> dict:
    """Injected into every template render so write UI can branch on role."""
    return {"is_viewer": is_viewer(request)}


def _ist_filter(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render a DB TIMESTAMPTZ in IST. asyncpg returns timezone-aware UTC datetimes,
    so a naive .strftime() would print UTC (−5:30) — e.g. an 11:00 IST trade as 05:30.
    This converts to IST first so every timestamp on the dashboard reads as IST."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime(fmt)


templates = Jinja2Templates(directory=str(TEMPLATES_DIR), context_processors=[_is_viewer_ctx])
templates.env.filters["ist"] = _ist_filter
app.state.templates = templates

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Auth gate — every page except /login and /health requires a session.
# Must be defined BEFORE SessionMiddleware is added so that SessionMiddleware
# wraps it (i.e. session is populated before auth_gate reads it).
@app.middleware("http")
async def auth_gate(request: Request, call_next):
    public_paths = {"/login", "/health"}
    if (request.url.path in public_paths
            or request.url.path.startswith("/static/")):
        return await call_next(request)
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)


# SessionMiddleware added last so it becomes the outermost layer.
# Starlette builds the stack inside-out: last add_middleware call = outermost.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=False,  # Caddy terminates TLS upstream; the cookie stays internal
    max_age=14 * 24 * 3600,
)


app.include_router(login.router)
app.include_router(health.router)
app.include_router(dashboard.router)
app.include_router(portfolios.router)
app.include_router(trades.router)
app.include_router(diagnose.router)
app.include_router(symbols.router)
app.include_router(bot.router)
app.include_router(api.router)
