"""FastAPI app — Kite-like paper-trading dashboard.

Routes are split across src/web/routes/. This file wires the app together.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from src.core.config import REPO_ROOT, settings
from src.core.db import close_pool, get_pool
from src.core.logging import setup_logging
from src.web.auth import is_authenticated
from src.web.routes import api, dashboard, diagnose, health, login, portfolios, trades


log = setup_logging("web")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    log.info("web app started")
    yield
    await close_pool()


app = FastAPI(lifespan=lifespan, title="Paper Trading", docs_url=None, redoc_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=False,  # Caddy terminates TLS upstream; the cookie stays internal
    max_age=14 * 24 * 3600,
)

# Jinja templates exposed on app.state so routes share one renderer.
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR    = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.state.templates = templates

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Auth gate — every page except /login and /health requires a session.
@app.middleware("http")
async def auth_gate(request: Request, call_next):
    public_paths = {"/login", "/health"}
    if (request.url.path in public_paths
            or request.url.path.startswith("/static/")):
        return await call_next(request)
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)


app.include_router(login.router)
app.include_router(health.router)
app.include_router(dashboard.router)
app.include_router(portfolios.router)
app.include_router(trades.router)
app.include_router(diagnose.router)
app.include_router(api.router)
