"""Login, logout, and token routes."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..auth import (
    MFA_PENDING_MINUTES,
    create_access_token,
    create_mfa_pending_token,
    create_oidc_flow_token,
    decode_mfa_pending_token,
    decode_oidc_flow_token,
    verify_password,
)
from ..mfa import (
    confirm_enrollment,
    decrypt_secret,
    get_enrollment,
    provisioning_uri,
    role_requires_mfa,
    start_enrollment,
    verify_login_code,
)
from ..config import cookie_secure
from ..deps import (
    _client_ip,
    _set_auth_cookie,
    get_db,
    get_optional_current_user,
    templates,
)
from ..models import User
from ..observability import incr
from ..ports.identity import (
    IdentityError,
    get_identity_port,
    get_ldap_adapter,
    ldap_enabled,
    ldap_local_fallback_allowed,
    oidc_enabled,
    pkce_pair,
)
from ..security import login_rate_limiter, new_csrf_token, set_csrf_cookie
from ..services.external_identity import resolve_external_user
from ..spa import safe_next

logger = logging.getLogger("bcp_project.routers.auth")
router = APIRouter()


LOGIN_NEXT_COOKIE = "login_next"


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Any:
    response = templates.TemplateResponse(
        request,
        "login.html",
        {"oidc_enabled": oidc_enabled()},
    )
    target = safe_next(request.query_params.get("next"))
    if target:
        response.set_cookie(
            LOGIN_NEXT_COOKIE, target, httponly=True, secure=cookie_secure(), samesite="lax", max_age=900, path="/"
        )
    return response


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    if login_rate_limiter.is_blocked(request, username):
        return RedirectResponse(url="/login?error=locked", status_code=status.HTTP_303_SEE_OTHER)

    try:
        user = await _authenticate_password(request, db, username, password)
    except IdentityError as exc:
        if exc.code == "invalid":
            login_rate_limiter.record_failure(request, username)
        elif exc.code == "ldap_unavailable":
            incr("auth_directory_errors")
        return RedirectResponse(url=f"/login?error={exc.code}", status_code=status.HTTP_303_SEE_OTHER)

    if role_requires_mfa(user.role):
        enrollment = await get_enrollment(db, user.username)
        target = "/login/mfa" if enrollment is not None and enrollment.enabled else "/login/mfa/setup"
        response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
        _set_mfa_pending_cookie(response, user.username)
        await write_audit(
            db,
            username=user.username,
            action="login_password_ok",
            resource_type="session",
            resource_id=user.username,
            detail="mfa_step_up",
            ip_address=_client_ip(request),
            commit=True,
        )
        return response

    login_rate_limiter.clear(request, username)
    return await _issue_session(request, db, user, mfa=False, action="login")


async def _local_user(db: AsyncSession, username: str, password: str) -> User:
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None or not verify_password(password, user.hashed_password):
        raise IdentityError("invalid")
    if not user.is_active:
        raise IdentityError("inactive")
    return user


async def _authenticate_password(request: Request, db: AsyncSession, username: str, password: str) -> User:
    """Directory bind when LDAP is on (with an allowlisted local break-glass), else the local password."""
    if not ldap_enabled():
        return await _local_user(db, username, password)
    adapter = get_ldap_adapter()
    try:
        identity = await asyncio.to_thread(adapter.authenticate, username, password)
    except ImportError:
        identity_error = IdentityError("ldap_unavailable", "ldap3 not installed")
    except IdentityError as exc:
        identity_error = exc
    except Exception as exc:  # network / server errors from ldap3
        logger.warning("LDAP authentication error: %s", exc)
        identity_error = IdentityError("ldap_unavailable", str(exc))
    else:
        return await resolve_external_user(
            db,
            identity,
            role_map=adapter.role_map,
            auto_provision=_truthy_env("LDAP_AUTO_PROVISION"),
            ip_address=_client_ip(request),
        )
    if identity_error.code in {"invalid", "ldap_unavailable"} and ldap_local_fallback_allowed(username):
        user = await _local_user(db, username, password)
        await write_audit(
            db,
            username=user.username,
            action="login_local_fallback",
            resource_type="session",
            resource_id=user.username,
            detail=f"ldap={identity_error.code}",
            ip_address=_client_ip(request),
            commit=True,
        )
        return user
    raise identity_error


def _truthy_env(name: str, default: str = "") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


MFA_PENDING_COOKIE = "mfa_pending"


def _set_mfa_pending_cookie(response: Response, username: str) -> None:
    response.set_cookie(
        key=MFA_PENDING_COOKIE,
        value=create_mfa_pending_token(username),
        httponly=True,
        secure=cookie_secure(),
        samesite="strict",
        max_age=MFA_PENDING_MINUTES * 60,
        path="/login",
    )


async def _pending_user(request: Request, db: AsyncSession) -> Optional[User]:
    token = request.cookies.get(MFA_PENDING_COOKIE)
    username = decode_mfa_pending_token(token) if token else None
    if not username:
        return None
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


async def _issue_session(
    request: Request, db: AsyncSession, user: User, *, mfa: bool, action: str, redirect_to: str = "/"
) -> RedirectResponse:
    token = create_access_token(subject=user.username, role=user.role.value, mfa=mfa)
    if redirect_to == "/":
        redirect_to = safe_next(request.cookies.get(LOGIN_NEXT_COOKIE)) or "/"
    response = RedirectResponse(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_cookie(response, token)
    set_csrf_cookie(response, new_csrf_token(), secure=cookie_secure())
    response.delete_cookie(MFA_PENDING_COOKIE, path="/login")
    response.delete_cookie(LOGIN_NEXT_COOKIE, path="/")
    incr("auth_logins", labels={"method": action})
    await write_audit(
        db,
        username=user.username,
        action=action,
        resource_type="session",
        resource_id=user.username,
        ip_address=_client_ip(request),
        actor_type="user",
        commit=True,
    )
    return response


@router.get("/login/mfa", response_class=HTMLResponse)
async def mfa_verify_page(request: Request, db: AsyncSession = Depends(get_db)) -> Any:
    user = await _pending_user(request, db)
    if user is None:
        return RedirectResponse(url="/login?error=mfa_expired", status_code=status.HTTP_303_SEE_OTHER)
    enrollment = await get_enrollment(db, user.username)
    if enrollment is None or not enrollment.enabled:
        return RedirectResponse(url="/login/mfa/setup", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "mfa.html", {"mode": "verify", "username": user.username})


@router.post("/login/mfa")
async def mfa_verify(request: Request, code: str = Form(...), db: AsyncSession = Depends(get_db)) -> Any:
    user = await _pending_user(request, db)
    if user is None:
        return RedirectResponse(url="/login?error=mfa_expired", status_code=status.HTTP_303_SEE_OTHER)
    if login_rate_limiter.is_blocked(request, user.username):
        return RedirectResponse(url="/login?error=locked", status_code=status.HTTP_303_SEE_OTHER)
    enrollment = await get_enrollment(db, user.username)
    if enrollment is None or not await verify_login_code(db, enrollment, code):
        login_rate_limiter.record_failure(request, user.username)
        await write_audit(
            db,
            username=user.username,
            action="mfa_failed",
            resource_type="session",
            resource_id=user.username,
            ip_address=_client_ip(request),
            commit=True,
        )
        return RedirectResponse(url="/login/mfa?error=invalid", status_code=status.HTTP_303_SEE_OTHER)
    login_rate_limiter.clear(request, user.username)
    return await _issue_session(request, db, user, mfa=True, action="login_mfa")


@router.get("/login/mfa/setup", response_class=HTMLResponse)
async def mfa_setup_page(request: Request, db: AsyncSession = Depends(get_db)) -> Any:
    user = await _pending_user(request, db)
    if user is None:
        return RedirectResponse(url="/login?error=mfa_expired", status_code=status.HTTP_303_SEE_OTHER)
    existing = await get_enrollment(db, user.username)
    if existing is not None and existing.enabled:
        return RedirectResponse(url="/login/mfa", status_code=status.HTTP_303_SEE_OTHER)
    _, secret = await start_enrollment(db, user)
    return templates.TemplateResponse(
        request,
        "mfa.html",
        {
            "mode": "setup",
            "username": user.username,
            "secret": secret,
            "otpauth_uri": provisioning_uri(secret, user.username),
        },
    )


@router.post("/login/mfa/setup", response_class=HTMLResponse)
async def mfa_setup_confirm(request: Request, code: str = Form(...), db: AsyncSession = Depends(get_db)) -> Any:
    user = await _pending_user(request, db)
    if user is None:
        return RedirectResponse(url="/login?error=mfa_expired", status_code=status.HTTP_303_SEE_OTHER)
    if login_rate_limiter.is_blocked(request, user.username):
        return RedirectResponse(url="/login?error=locked", status_code=status.HTTP_303_SEE_OTHER)
    enrollment = await get_enrollment(db, user.username)
    if enrollment is None or enrollment.enabled:
        return RedirectResponse(url="/login/mfa", status_code=status.HTTP_303_SEE_OTHER)
    recovery_codes = await confirm_enrollment(db, enrollment, code)
    if recovery_codes is None:
        login_rate_limiter.record_failure(request, user.username)
        secret = decrypt_secret(enrollment.secret_encrypted) or ""
        return templates.TemplateResponse(
            request,
            "mfa.html",
            {
                "mode": "setup",
                "username": user.username,
                "secret": secret,
                "otpauth_uri": provisioning_uri(secret, user.username),
                "error": "invalid",
            },
            status_code=400,
        )
    login_rate_limiter.clear(request, user.username)
    await write_audit(
        db,
        username=user.username,
        action="mfa_enrolled",
        resource_type="user",
        resource_id=user.username,
        ip_address=_client_ip(request),
        commit=True,
    )
    session = await _issue_session(request, db, user, mfa=True, action="login_mfa")
    page = templates.TemplateResponse(
        request,
        "mfa.html",
        {"mode": "recovery", "username": user.username, "recovery_codes": recovery_codes},
    )
    for key, value in session.headers.items():
        if key.lower() == "set-cookie":
            page.headers.append("set-cookie", value)
    page.headers["Cache-Control"] = "no-store"
    return page


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
    response.delete_cookie(MFA_PENDING_COOKIE, path="/login")
    return response


@router.post("/token")
async def token(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)) -> Any:
    if login_rate_limiter.is_blocked(request, form_data.username):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts")
    try:
        user = await _authenticate_password(request, db, form_data.username, form_data.password)
    except IdentityError as exc:
        if exc.code == "invalid":
            login_rate_limiter.record_failure(request, form_data.username)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password")
        if exc.code == "inactive":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User account is inactive")
        if exc.code == "ldap_unavailable":
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Directory unavailable")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No archive role granted")
    mfa_ok = False
    if role_requires_mfa(user.role):
        enrollment = await get_enrollment(db, user.username)
        if enrollment is None or not enrollment.enabled:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Enrol MFA via the web sign-in first")
        otp = (request.headers.get("x-otp") or "").strip()
        if not otp or not await verify_login_code(db, enrollment, otp):
            login_rate_limiter.record_failure(request, form_data.username)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Valid X-OTP header required")
        mfa_ok = True
    login_rate_limiter.clear(request, form_data.username)
    token = create_access_token(subject=user.username, role=user.role.value, mfa=mfa_ok)
    return {"access_token": token, "token_type": "bearer"}


OIDC_FLOW_COOKIE = "oidc_flow"


def _oidc_fail(code: str) -> RedirectResponse:
    response = RedirectResponse(url=f"/login?error={code}", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(OIDC_FLOW_COOKIE, path="/auth/oidc")
    return response


@router.get("/auth/oidc/start")
async def oidc_start(request: Request) -> RedirectResponse:
    if not oidc_enabled():
        raise HTTPException(status_code=404, detail="OIDC is not enabled")
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    redirect_uri = str(request.url_for("oidc_callback"))
    url = await asyncio.to_thread(
        get_identity_port().authorization_url,
        state=state,
        redirect_uri=redirect_uri,
        nonce=nonce,
        code_challenge=challenge,
    )
    if not url:
        raise HTTPException(status_code=503, detail="OIDC authorize URL not configured")
    response = RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=OIDC_FLOW_COOKIE,
        value=create_oidc_flow_token(state, nonce, verifier),
        httponly=True,
        secure=cookie_secure(),
        samesite="lax",
        max_age=600,
        path="/auth/oidc",
    )
    return response


@router.get("/auth/oidc/callback", name="oidc_callback")
async def oidc_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Exchange the code (with the PKCE verifier), verify the ID token, then map it to a local user."""
    if not oidc_enabled():
        raise HTTPException(status_code=404, detail="OIDC is not enabled")
    flow = decode_oidc_flow_token(request.cookies.get(OIDC_FLOW_COOKIE) or "")
    if not code or not state or not flow or not secrets.compare_digest(state, str(flow.get("state") or "")):
        return _oidc_fail("oidc_state")

    port = get_identity_port()
    redirect_uri = str(request.url_for("oidc_callback"))
    try:
        tokens = await asyncio.to_thread(
            port.exchange_code, code=code, redirect_uri=redirect_uri, code_verifier=str(flow["verifier"])
        )
        claims = await asyncio.to_thread(
            port.verify_id_token, tokens["id_token"], nonce=str(flow["nonce"]), access_token=tokens.get("access_token")
        )
        identity = port.identity_from_claims(claims)
        user = await resolve_external_user(
            db,
            identity,
            role_map=port.role_map,
            auto_provision=_truthy_env("OIDC_AUTO_PROVISION"),
            ip_address=_client_ip(request),
        )
    except IdentityError as exc:
        logger.warning("OIDC sign-in rejected (%s): %s", exc.code, exc)
        await write_audit(
            db,
            username="anonymous",
            action="login_oidc_rejected",
            resource_type="session",
            resource_id=exc.code,
            detail=str(exc)[:500],
            ip_address=_client_ip(request),
            commit=True,
        )
        return _oidc_fail(exc.code)

    # The IdP's MFA counts only when trusted and asserted in the ID token's amr claim.
    idp_mfa = _truthy_env("OIDC_TRUST_IDP_MFA", "1") and identity.mfa
    if role_requires_mfa(user.role) and not idp_mfa:
        enrollment = await get_enrollment(db, user.username)
        target = "/login/mfa" if enrollment is not None and enrollment.enabled else "/login/mfa/setup"
        response = RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)
        _set_mfa_pending_cookie(response, user.username)
        response.delete_cookie(OIDC_FLOW_COOKIE, path="/auth/oidc")
        await write_audit(
            db,
            username=user.username,
            action="login_oidc_ok",
            resource_type="session",
            resource_id=user.username,
            detail="mfa_step_up",
            ip_address=_client_ip(request),
            commit=True,
        )
        return response
    response = await _issue_session(request, db, user, mfa=idp_mfa, action="login_oidc")
    response.delete_cookie(OIDC_FLOW_COOKIE, path="/auth/oidc")
    return response
