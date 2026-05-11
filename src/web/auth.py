"""Two-role session auth.

`DASHBOARD_PASSWORD` grants full edit access (role='admin').
Optional `VIEWER_PASSWORD` grants a read-only session (role='viewer') that
can be shared with friends. Mutation endpoints call `require_admin()`;
templates branch on `is_viewer` to hide write UI.

Constant-time string compare to avoid trivial timing leaks."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from src.core.config import settings


SESSION_KEY = "auth_ok"
ROLE_KEY = "role"

ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def role(request: Request) -> str | None:
    return request.session.get(ROLE_KEY)


def is_viewer(request: Request) -> bool:
    return role(request) == ROLE_VIEWER


def check_password(candidate: str) -> str | None:
    """Return the matching role, or None if the password is wrong."""
    if hmac.compare_digest(candidate, settings.dashboard_password):
        return ROLE_ADMIN
    viewer_pw = settings.viewer_password
    if viewer_pw and hmac.compare_digest(candidate, viewer_pw):
        return ROLE_VIEWER
    return None


def login(request: Request, role_value: str) -> None:
    request.session[SESSION_KEY] = True
    request.session[ROLE_KEY] = role_value


def logout(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)
    request.session.pop(ROLE_KEY, None)


def require_admin(request: Request) -> None:
    """Raise 403 if the current session is not an admin. Call at the top of
    every mutation endpoint (POST / PUT / DELETE that changes state)."""
    if role(request) != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="Read-only session — admin access required.")
