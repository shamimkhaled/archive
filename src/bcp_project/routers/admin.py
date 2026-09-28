"""Admin user management routes."""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..auth import get_password_hash
from ..deps import _client_ip, get_current_user, get_db, require_role, templates
from ..models import Role, User

logger = logging.getLogger("bcp_project.routers.admin")
router = APIRouter()

@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
    error: Optional[str] = None,
) -> Any:
    require_role(current_user, [Role.admin])

    statement = select(User).order_by(User.created_at.desc())
    result = await db.execute(statement)
    users = result.scalars().all()

    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "user": current_user,
            "users": users,
            "roles": list(Role),
            "status": status,
            "error": error,
            "form_values": {
                "username": request.query_params.get("username", ""),
                "email": request.query_params.get("email", ""),
                "role": request.query_params.get("role", ""),
            },
            "field_errors": {
                "username": request.query_params.get("err_username", ""),
                "password": request.query_params.get("err_password", ""),
                "email": request.query_params.get("err_email", ""),
                "role": request.query_params.get("err_role", ""),
            },
        },
    )


def _admin_users_error_redirect(**parts: str) -> RedirectResponse:
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in parts.items() if v})
    return RedirectResponse(url=f"/admin/users?{query}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/users")
async def admin_create_user(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    role: str = Form(""),
    email: str = Form(""),
    notifications_enabled: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])

    username = (username or "").strip()
    password = password or ""
    email = (email or "").strip()
    role_raw = (role or "").strip()
    err_username = err_password = err_email = err_role = ""

    if len(username) < 3 or len(username) > 64:
        err_username = "Enter a username between 3 and 64 characters."
    elif not all(ch.isalnum() or ch in "_-" for ch in username):
        err_username = "Use letters, numbers, hyphens, or underscores only."

    if len(password) < 8:
        err_password = "Password must be at least 8 characters."

    if email and ("@" not in email or "." not in email.split("@")[-1]):
        err_email = "Enter a valid email address, or leave it blank."

    try:
        role_value = Role(role_raw) if role_raw else None
    except ValueError:
        role_value = None
    if role_value is None:
        err_role = "Select a role for this user."

    if err_username or err_password or err_email or err_role:
        return _admin_users_error_redirect(
            error="Please fix the highlighted fields and try again.",
            username=username,
            email=email,
            role=role_raw,
            err_username=err_username,
            err_password=err_password,
            err_email=err_email,
            err_role=err_role,
        )

    statement = select(User).where(User.username == username)
    result = await db.execute(statement)
    if result.scalar_one_or_none() is not None:
        return _admin_users_error_redirect(
            error="That username is already taken. Choose another.",
            username=username,
            email=email,
            role=role_raw,
            err_username="Username already exists.",
        )

    try:
        hashed = get_password_hash(password)
    except Exception:
        logger.exception("Password hashing failed for new user %s", username)
        return _admin_users_error_redirect(
            error="Could not save this password. Try a different one.",
            username=username,
            email=email,
            role=role_raw,
            err_password="Password could not be saved. Try another password.",
        )

    new_user = User(
        username=username,
        email=email or None,
        hashed_password=hashed,
        role=role_value,
        is_active=True,
        notifications_enabled=notifications_enabled is not None,
    )
    db.add(new_user)
    await write_audit(
        db,
        username=current_user.username,
        action="user_create",
        resource_type="user",
        resource_id=username,
        detail=f"role={role_value.value}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()

    return RedirectResponse(url="/admin/users?status=created", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/users/{user_id}/update")
async def admin_update_user(
    request: Request,
    user_id: int,
    username: str = Form(""),
    email: str = Form(""),
    role: str = Form(...),
    is_active: Optional[str] = Form(None),
    notifications_enabled: Optional[str] = Form(None),
    new_password: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    statement = select(User).where(User.id == user_id)
    result = await db.execute(statement)
    target = result.scalar_one_or_none()
    if target is None:
        return RedirectResponse(url="/admin/users?error=User+not+found", status_code=303)

    username = (username or "").strip() or target.username
    if len(username) < 3 or len(username) > 64:
        return RedirectResponse(
            url="/admin/users?error=Username+must+be+3–64+characters",
            status_code=303,
        )
    if not all(ch.isalnum() or ch in "_-" for ch in username):
        return RedirectResponse(
            url="/admin/users?error=Username+may+only+use+letters,+numbers,+hyphens,+underscores",
            status_code=303,
        )
    if username != target.username:
        taken = await db.execute(select(User).where(User.username == username, User.id != target.id))
        if taken.scalar_one_or_none() is not None:
            return RedirectResponse(url="/admin/users?error=That+username+is+already+taken", status_code=303)
        target.username = username

    try:
        target.role = Role(role.strip())
    except ValueError:
        return RedirectResponse(url="/admin/users?error=Invalid+role", status_code=303)

    email = email.strip()
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        return RedirectResponse(url="/admin/users?error=Invalid+email+address", status_code=303)
    target.email = email or None
    target.notifications_enabled = notifications_enabled is not None

    if target.id == current_user.id:
        target.is_active = True
        target.role = Role.admin
    else:
        target.is_active = is_active is not None

    if new_password.strip():
        if len(new_password.strip()) < 8:
            return RedirectResponse(
                url="/admin/users?error=New+password+must+be+at+least+8+characters",
                status_code=303,
            )
        target.hashed_password = get_password_hash(new_password.strip())

    await write_audit(
        db,
        username=current_user.username,
        action="user_update",
        resource_type="user",
        resource_id=target.username,
        detail=f"role={target.role.value};active={target.is_active}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    return RedirectResponse(url="/admin/users?status=updated", status_code=303)


@router.post("/admin/users/{user_id}/delete")
async def admin_delete_user(
    request: Request,
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    if user_id == current_user.id:
        return RedirectResponse(url="/admin/users?error=You+cannot+delete+your+own+account", status_code=303)

    statement = select(User).where(User.id == user_id)
    result = await db.execute(statement)
    target = result.scalar_one_or_none()
    if target is None:
        return RedirectResponse(url="/admin/users?error=User+not+found", status_code=303)

    username = target.username
    await db.delete(target)
    await write_audit(
        db,
        username=current_user.username,
        action="user_delete",
        resource_type="user",
        resource_id=username,
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    return RedirectResponse(url="/admin/users?status=deleted", status_code=303)
