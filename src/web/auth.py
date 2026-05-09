"""Single-password session auth.

Compare against `DASHBOARD_PASSWORD` from .env. On success, set a session flag.
Constant-time string compare to avoid trivial timing leaks."""

from __future__ import annotations

import hmac

from fastapi import Request

from src.core.config import settings


SESSION_KEY = "auth_ok"


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def check_password(candidate: str) -> bool:
    return hmac.compare_digest(candidate, settings.dashboard_password)


def login(request: Request) -> None:
    request.session[SESSION_KEY] = True


def logout(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)
