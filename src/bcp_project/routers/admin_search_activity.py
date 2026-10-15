"""Admin view of the search activity log: who searched for what, what came back, what the AI used."""
from __future__ import annotations

import csv
import io
from typing import Any, Dict, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..deps import _client_ip, get_current_user, get_db, require_role, templates
from ..models import Role, User
from ..security import spreadsheet_safe
from ..services.search_log import (
    CHANNEL_LABELS,
    CSV_COLUMNS,
    ActivityQuery,
    activity_dict,
    csv_row,
    list_activity,
    search_log_retention_days,
    titles_for,
)

router = APIRouter()

PER_PAGE = 50
MAX_EXPORT_ROWS = 50_000


def _query_from_request(request: Request) -> ActivityQuery:
    params = request.query_params
    try:
        return ActivityQuery.from_params(
            username=params.get("username"),
            channel=params.get("channel"),
            q=params.get("q"),
            doc_id=params.get("doc_id"),
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
            refused_only=params.get("refused_only"),
            restricted_only=params.get("restricted_only"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _describe(query: ActivityQuery) -> str:
    return " ".join(f"{k}={v}" for k, v in query.as_params().items())[:400] or "all"


async def _page(db: AsyncSession, query: ActivityQuery, page: int) -> Dict[str, Any]:
    rows, total = await list_activity(db, query, page=page, per_page=PER_PAGE)
    titles = await titles_for(db, rows)
    entries = [{**activity_dict(row, titles), "created": row.created_at} for row in rows]
    return {"entries": entries, "total": total, "page": page, "pages": max(1, -(-total // PER_PAGE))}


@router.get("/admin/search-activity", response_class=HTMLResponse)
async def admin_search_activity_page(
    request: Request,
    page: int = Query(1, ge=1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin])
    query = _query_from_request(request)
    data = await _page(db, query, page)
    await write_audit(
        db,
        username=current_user.username,
        action="search_log_view",
        resource_type="search_activity",
        detail=f"page={page} {_describe(query)}",
        ip_address=_client_ip(request),
        actor_type="user",
        commit=True,
    )
    params = query.as_params()
    return templates.TemplateResponse(
        request,
        "admin_search_activity.html",
        {
            "user": current_user,
            "page_title": "Search activity",
            "query": query,
            "params": params,
            "channels": CHANNEL_LABELS,
            "retention_days": search_log_retention_days(),
            **data,
        },
    )


@router.get("/api/admin/search-activity")
async def api_admin_search_activity(
    request: Request,
    page: int = Query(1, ge=1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    require_role(current_user, [Role.admin])
    query = _query_from_request(request)
    data = await _page(db, query, page)
    await write_audit(
        db,
        username=current_user.username,
        action="search_log_view",
        resource_type="search_activity",
        detail=f"api page={page} {_describe(query)}",
        ip_address=_client_ip(request),
        actor_type="user",
        commit=True,
    )
    for entry in data["entries"]:
        entry.pop("created", None)
    return {**data, "per_page": PER_PAGE, "filters": query.as_params()}


@router.get("/admin/search-activity.csv")
async def admin_search_activity_csv(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin])
    query = _query_from_request(request)
    rows, total = await list_activity(db, query, page=1, per_page=MAX_EXPORT_ROWS)
    await write_audit(
        db,
        username=current_user.username,
        action="search_log_export",
        resource_type="search_activity",
        detail=f"rows={len(rows)} of={total} {_describe(query)}",
        ip_address=_client_ip(request),
        actor_type="user",
        commit=True,
    )

    def generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow([spreadsheet_safe(v) for v in csv_row(row)])
            if buffer.tell() > 64_000:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
        yield buffer.getvalue()

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="search-activity.csv"', "Cache-Control": "no-store"},
    )
