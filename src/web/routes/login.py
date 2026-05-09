"""Login + logout routes."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.web import auth


router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    if auth.is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return request.app.state.templates.TemplateResponse(
        "login.html", {"request": request, "error": None}
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)) -> HTMLResponse:
    if not auth.check_password(password):
        return request.app.state.templates.TemplateResponse(
            "login.html", {"request": request, "error": "Incorrect password."},
            status_code=401,
        )
    auth.login(request)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    auth.logout(request)
    return RedirectResponse("/login", status_code=303)
