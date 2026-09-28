"""Login, logout, and token routes."""
from __future__ import annotations

import os
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..auth import create_access_token, verify_password
from ..config import cookie_secure
from ..deps import (
    _client_ip,
    _set_auth_cookie,
    get_db,
    get_optional_current_user,
    templates,
)
from ..models import User
from ..ports.identity import get_identity_port, oidc_enabled
from ..security import login_rate_limiter, new_csrf_token, set_csrf_cookie

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Any:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"oidc_enabled": oidc_enabled()},
    )


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    if login_rate_limiter.is_blocked(request, username):
        return RedirectResponse(url="/login?error=locked", status_code=status.HTTP_303_SEE_OTHER)

    statement = select(User).where(User.username == username)
    result = await db.execute(statement)
    user = result.scalar_one_or_none()
    if user is None or not verify_password(password, user.hashed_password):
        login_rate_limiter.record_failure(request, username)
        return RedirectResponse(url="/login?error=invalid", status_code=status.HTTP_303_SEE_OTHER)
    if not user.is_active:
        return RedirectResponse(url="/login?error=inactive", status_code=status.HTTP_303_SEE_OTHER)

    login_rate_limiter.clear(request, username)
    token = create_access_token(subject=user.username, role=user.role.value)
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(response, token)
    set_csrf_cookie(response, new_csrf_token(), secure=cookie_secure())
    await write_audit(
        db,
        username=user.username,
        action="login",
        resource_type="session",
        resource_id=user.username,
        ip_address=_client_ip(request),
        commit=True,
    )
    return response


@router.get("/logout")
async def logout(request: Request, current_user: Optional[User] = Depends(get_optional_current_user), db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    if current_user is not None:
        await write_audit(
            db,
            username=current_user.username,
            action="logout",
            resource_type="session",
            resource_id=current_user.username,
            ip_address=_client_ip(request),
            commit=True,
        )
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("csrf_token", path="/")
    return response


@router.post("/token")
async def token(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)) -> Any:
    if login_rate_limiter.is_blocked(request, form_data.username):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts")
    statement = select(User).where(User.username == form_data.username)
    result = await db.execute(statement)
    user = result.scalar_one_or_none()
    if user is None or not verify_password(form_data.password, user.hashed_password):
        login_rate_limiter.record_failure(request, form_data.username)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User account is inactive")
    login_rate_limiter.clear(request, form_data.username)
    token = create_access_token(subject=user.username, role=user.role.value)
    return {"access_token": token, "token_type": "bearer"}


@router.get("/auth/oidc/start")
async def oidc_start(request: Request) -> RedirectResponse:
    if not oidc_enabled():
        raise HTTPException(status_code=404, detail="OIDC is not enabled")
    state = secrets.token_urlsafe(24)
    redirect_uri = str(request.url_for("oidc_callback"))
    url = get_identity_port().authorization_url(state=state, redirect_uri=redirect_uri)
    if not url:
        raise HTTPException(status_code=503, detail="OIDC authorize URL not configured")
    response = RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="oidc_state",
        value=state,
        httponly=True,
        secure=cookie_secure(),
        samesite="lax",
        max_age=600,
        path="/",
    )
    return response


@router.get("/auth/oidc/callback", name="oidc_callback")
async def oidc_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """OIDC scaffolding: exchanges code, then requires a pre-provisioned local user.

    Full IdP auto-provisioning / group→role mapping is a bank-specific Phase 3+ task.
    """
    if not oidc_enabled():
        raise HTTPException(status_code=404, detail="OIDC is not enabled")
    expected = request.cookies.get("oidc_state")
    if not code or not state or not expected or not secrets.compare_digest(state, expected):
        return RedirectResponse(url="/login?error=oidc_state", status_code=status.HTTP_303_SEE_OTHER)

    redirect_uri = str(request.url_for("oidc_callback"))
    try:
        token_payload = get_identity_port().exchange_code(code=code, redirect_uri=redirect_uri)
    except Exception:
        return RedirectResponse(url="/login?error=oidc_token", status_code=status.HTTP_303_SEE_OTHER)
    if not token_payload:
        return RedirectResponse(url="/login?error=oidc_token", status_code=status.HTTP_303_SEE_OTHER)

    # Prefer explicit claim mapping via env; otherwise require ?username= for lab wiring.
    preferred = (request.query_params.get("username") or "").strip()
    claim_user = preferred or (os.getenv("OIDC_LAB_USERNAME") or "").strip()
    if not claim_user:
        return RedirectResponse(url="/login?error=oidc_map", status_code=status.HTTP_303_SEE_OTHER)

    statement = select(User).where(User.username == claim_user)
    result = await db.execute(statement)
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        return RedirectResponse(url="/login?error=oidc_user", status_code=status.HTTP_303_SEE_OTHER)

    token = create_access_token(subject=user.username, role=user.role.value)
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(response, token)
    set_csrf_cookie(response, new_csrf_token(), secure=cookie_secure())
    response.delete_cookie("oidc_state", path="/")
    await write_audit(
        db,
        username=user.username,
        action="login_oidc",
        resource_type="session",
        resource_id=user.username,
        ip_address=_client_ip(request),
        actor_type="user",
        commit=True,
    )
    return response
