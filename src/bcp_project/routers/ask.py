"""Ask Sonali Bank UI routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ..deps import get_current_user, require_role, templates
from ..models import Role, User

router = APIRouter()


@router.get("/ask", response_class=HTMLResponse)
async def ask_page(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    return templates.TemplateResponse(
        request,
        "ask.html",
        {"user": current_user},
    )
