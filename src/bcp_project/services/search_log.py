"""Search activity log: one writer for every search channel and Ask Sonali, plus admin queries.

Each row is linked to a short hash-chained ``audit_logs`` row (``search_logged``) carrying the
row's sha256, so the existing chain verification and SIEM forwarding cover the log too.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..correlation import get_correlation_id
from ..models import DocumentRecord, SearchActivity, Sensitivity

logger = logging.getLogger("bcp_project.services.search_log")

CHANNELS = ("web_search", "metadata_search", "api_v1", "mcp", "ask", "briefing_refs")
CHANNEL_LABELS = {
    "web_search": "Archive search",
    "metadata_search": "Filter-only search",
    "api_v1": "API",
    "mcp": "AI agent tool",
    "ask": "Ask Sonali",
    "briefing_refs": "Briefing references",
}
MAX_QUERY_CHARS = 2000
MAX_LOGGED_RESULTS = 20
HASHED_FIELDS = (
    "created_at", "username", "actor_type", "role", "channel", "tool_name", "ip_address",
    "correlation_id", "query", "lang", "filters", "mode", "cached", "degraded", "latency_ms",
    "result_count", "viewable_count", "redacted_count", "restricted_count", "results", "doc_ids",
    "ai_provider", "ai_model", "ai_refused", "ai_refusal_reason", "ai_source_docs",
    "ai_cited_docs", "ai_withheld_docs",
)


def search_log_retention_days() -> int:
    """0 keeps the log forever."""
    try:
        return max(0, int(os.getenv("SEARCH_LOG_RETENTION_DAYS", "0") or 0))
    except ValueError:
        return 0


def _role_value(role: Any) -> Optional[str]:
    return getattr(role, "value", role) if role else None


def _result_entry(rank: int, row: Dict[str, Any]) -> Dict[str, Any]:
    score = row.get("score")
    if score is None:
        score = row.get("rerank_score", row.get("fused_score"))
    try:
        score = round(float(score), 4) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return {
        "doc_id": str(row.get("doc_id") or ""),
        "rank": rank,
        "score": score,
        "can_view": bool(row.get("can_view", True)),
        "page": row.get("page"),
    }


def _doc_refs(rows: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for row in rows or []:
        doc_id = str(row.get("doc_id") or "")
        if not doc_id:
            continue
        key = (doc_id, row.get("page"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"doc_id": doc_id, "page": row.get("page")})
    return out


def activity_hash(activity: SearchActivity) -> str:
    """sha256 over the logged content; recomputing it from the row proves it was not edited."""
    payload = {}
    for name in HASHED_FIELDS:
        value = getattr(activity, name)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        payload[name] = value
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class AiTrace:
    """What an AI answer drew on: passages given to the model, citations kept, evidence withheld."""

    provider: Optional[str] = None
    model: Optional[str] = None
    refused: bool = False
    refusal_reason: Optional[str] = None
    source_docs: Sequence[Dict[str, Any]] = ()
    cited_docs: Sequence[Dict[str, Any]] = ()
    withheld_docs: Sequence[Dict[str, Any]] = ()


async def record_search(
    db: AsyncSession,
    *,
    channel: str,
    username: str,
    query: str,
    results: Sequence[Dict[str, Any]] = (),
    actor_type: Optional[str] = "user",
    role: Any = None,
    tool_name: Optional[str] = None,
    ip_address: Optional[str] = None,
    lang: Optional[str] = None,
    filters: Optional[Dict[str, Any]] = None,
    mode: Optional[str] = None,
    cached: bool = False,
    degraded: bool = False,
    latency_ms: Optional[int] = None,
    ai_trace: Optional[AiTrace] = None,
    commit: bool = True,
) -> SearchActivity:
    rows = list(results or [])
    viewable = sum(1 for row in rows if row.get("can_view", True))
    restricted = sum(1 for row in rows if row.get("sensitivity") == Sensitivity.restricted.value)
    logged = [_result_entry(rank, row) for rank, row in enumerate(rows[:MAX_LOGGED_RESULTS], start=1)]
    activity = SearchActivity(
        created_at=datetime.utcnow().replace(microsecond=0),
        username=(username or "unknown")[:64],
        actor_type=actor_type,
        role=_role_value(role),
        channel=channel if channel in CHANNELS else "web_search",
        tool_name=tool_name,
        ip_address=ip_address,
        correlation_id=get_correlation_id(),
        query=(query or "")[:MAX_QUERY_CHARS],
        lang=(lang or None),
        filters={k: v for k, v in (filters or {}).items() if v not in (None, "", [], ())} or None,
        mode=mode,
        cached=bool(cached),
        degraded=bool(degraded),
        latency_ms=latency_ms,
        result_count=len(rows),
        viewable_count=viewable,
        redacted_count=len(rows) - viewable,
        restricted_count=restricted,
        results=logged,
    )
    doc_ids = {entry["doc_id"] for entry in logged if entry["doc_id"]}
    if ai_trace is not None:
        activity.ai_provider = ai_trace.provider
        activity.ai_model = ai_trace.model
        activity.ai_refused = bool(ai_trace.refused)
        activity.ai_refusal_reason = ai_trace.refusal_reason
        activity.ai_source_docs = _doc_refs(ai_trace.source_docs)
        activity.ai_cited_docs = _doc_refs(ai_trace.cited_docs)
        activity.ai_withheld_docs = _doc_refs(ai_trace.withheld_docs)
        for group in (activity.ai_source_docs, activity.ai_cited_docs, activity.ai_withheld_docs):
            doc_ids.update(ref["doc_id"] for ref in group)
    activity.doc_ids = (" " + " ".join(sorted(doc_ids)) + " ") if doc_ids else None
    activity.row_sha256 = activity_hash(activity)
    db.add(activity)
    await db.flush()

    detail = (
        f"channel={activity.channel} results={activity.result_count} viewable={activity.viewable_count}"
        f" sha256={activity.row_sha256}"
    )
    if ai_trace is not None:
        detail += f" refused={activity.ai_refused} cited={len(activity.ai_cited_docs or [])}"
    audit = await write_audit(
        db,
        username=activity.username,
        action="search_logged",
        resource_type="search_activity",
        resource_id=str(activity.id),
        detail=detail,
        ip_address=ip_address,
        actor_type=actor_type,
        tool_name=tool_name,
    )
    await db.flush()
    activity.audit_log_id = audit.id
    if commit:
        await db.commit()
    return activity


@dataclass
class ActivityQuery:
    username: str = ""
    channel: str = ""
    q: str = ""
    doc_id: str = ""
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    refused_only: bool = False
    restricted_only: bool = False

    @classmethod
    def from_params(
        cls,
        *,
        username: Optional[str] = None,
        channel: Optional[str] = None,
        q: Optional[str] = None,
        doc_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        refused_only: Any = None,
        restricted_only: Any = None,
    ) -> "ActivityQuery":
        def parse(value: Optional[str], name: str) -> Optional[date]:
            if not value:
                return None
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError as exc:
                raise ValueError(f"Invalid {name}, expected YYYY-MM-DD") from exc

        def flag(value: Any) -> bool:
            return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

        chan = (channel or "").strip()
        return cls(
            username=(username or "").strip()[:64],
            channel=chan if chan in CHANNELS else "",
            q=(q or "").strip()[:200],
            doc_id=(doc_id or "").strip()[:128],
            date_from=parse(date_from, "date_from"),
            date_to=parse(date_to, "date_to"),
            refused_only=flag(refused_only),
            restricted_only=flag(restricted_only),
        )

    def as_params(self) -> Dict[str, str]:
        out = {
            "username": self.username,
            "channel": self.channel,
            "q": self.q,
            "doc_id": self.doc_id,
            "date_from": self.date_from.isoformat() if self.date_from else "",
            "date_to": self.date_to.isoformat() if self.date_to else "",
            "refused_only": "1" if self.refused_only else "",
            "restricted_only": "1" if self.restricted_only else "",
        }
        return {k: v for k, v in out.items() if v}

    def conditions(self) -> List[Any]:
        conditions: List[Any] = []
        if self.username:
            conditions.append(func.lower(SearchActivity.username) == self.username.lower())
        if self.channel:
            conditions.append(SearchActivity.channel == self.channel)
        if self.q:
            conditions.append(func.lower(SearchActivity.query).contains(self.q.lower(), autoescape=True))
        if self.doc_id:
            conditions.append(SearchActivity.doc_ids.contains(f" {self.doc_id} ", autoescape=True))
        if self.date_from:
            conditions.append(SearchActivity.created_at >= datetime.combine(self.date_from, datetime.min.time()))
        if self.date_to:
            conditions.append(SearchActivity.created_at < datetime.combine(self.date_to + timedelta(days=1), datetime.min.time()))
        if self.refused_only:
            conditions.append(SearchActivity.ai_refused.is_(True))
        if self.restricted_only:
            conditions.append(or_(SearchActivity.restricted_count > 0, SearchActivity.redacted_count > 0))
        return conditions


async def list_activity(
    db: AsyncSession, query: ActivityQuery, *, page: int = 1, per_page: int = 50
) -> Tuple[List[SearchActivity], int]:
    conditions = query.conditions()
    where = and_(*conditions) if conditions else None
    count_stmt = select(func.count(SearchActivity.id))
    stmt = select(SearchActivity).order_by(SearchActivity.id.desc())
    if where is not None:
        count_stmt = count_stmt.where(where)
        stmt = stmt.where(where)
    total = int((await db.execute(count_stmt)).scalar() or 0)
    page = max(1, page)
    rows = (await db.execute(stmt.offset((page - 1) * per_page).limit(per_page))).scalars().all()
    return list(rows), total


async def activity_since(
    db: AsyncSession, query: ActivityQuery, *, since_id: int = 0, limit: int = 200
) -> List[SearchActivity]:
    stmt = (
        select(SearchActivity)
        .where(SearchActivity.id > since_id, *query.conditions())
        .order_by(SearchActivity.id.asc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def titles_for(db: AsyncSession, rows: Iterable[SearchActivity]) -> Dict[str, str]:
    ids = set()
    for row in rows:
        ids.update((row.doc_ids or "").split())
    if not ids:
        return {}
    result = await db.execute(select(DocumentRecord.doc_id, DocumentRecord.title).where(DocumentRecord.doc_id.in_(ids)))
    return {doc_id: title for doc_id, title in result.all() if title}


def activity_dict(row: SearchActivity, titles: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    titles = titles or {}

    def with_titles(refs: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        return [{**ref, "title": titles.get(ref.get("doc_id"))} for ref in refs or []]

    out = {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "username": row.username,
        "actor_type": row.actor_type,
        "role": row.role,
        "channel": row.channel,
        "channel_label": CHANNEL_LABELS.get(row.channel, row.channel),
        "tool_name": row.tool_name,
        "ip_address": row.ip_address,
        "correlation_id": row.correlation_id,
        "query": row.query,
        "lang": row.lang,
        "filters": row.filters or {},
        "mode": row.mode,
        "cached": bool(row.cached),
        "degraded": bool(row.degraded),
        "latency_ms": row.latency_ms,
        "result_count": row.result_count,
        "viewable_count": row.viewable_count,
        "redacted_count": row.redacted_count,
        "restricted_count": row.restricted_count,
        "results": with_titles(row.results),
        "row_sha256": row.row_sha256,
        "audit_log_id": row.audit_log_id,
        "intact": activity_hash(row) == row.row_sha256 if row.row_sha256 else None,
    }
    if row.channel == "ask" or row.ai_refused is not None:
        out["ai"] = {
            "provider": row.ai_provider,
            "model": row.ai_model,
            "refused": row.ai_refused,
            "refusal_reason": row.ai_refusal_reason,
            "source_docs": with_titles(row.ai_source_docs),
            "cited_docs": with_titles(row.ai_cited_docs),
            "withheld_docs": with_titles(row.ai_withheld_docs),
        }
    return out


CSV_COLUMNS = (
    "id", "created_at", "username", "role", "actor_type", "channel", "tool_name", "ip_address",
    "query", "filters", "result_count", "viewable_count", "redacted_count", "restricted_count",
    "result_doc_ids", "ai_model", "ai_refused", "ai_source_docs", "ai_cited_docs", "ai_withheld_docs",
    "row_sha256", "audit_log_id",
)


def csv_row(row: SearchActivity) -> List[Any]:
    def refs(items: Optional[List[Dict[str, Any]]]) -> str:
        return " ".join(
            f"{ref.get('doc_id')}" + (f"@p{ref['page']}" if ref.get("page") else "") for ref in items or []
        )

    return [
        row.id,
        row.created_at.isoformat() if row.created_at else "",
        row.username,
        row.role or "",
        row.actor_type or "",
        row.channel,
        row.tool_name or "",
        row.ip_address or "",
        row.query,
        json.dumps(row.filters or {}, ensure_ascii=False, sort_keys=True),
        row.result_count,
        row.viewable_count,
        row.redacted_count,
        row.restricted_count,
        refs(row.results),
        row.ai_model or "",
        "" if row.ai_refused is None else str(bool(row.ai_refused)).lower(),
        refs(row.ai_source_docs),
        refs(row.ai_cited_docs),
        refs(row.ai_withheld_docs),
        row.row_sha256 or "",
        row.audit_log_id or "",
    ]


async def purge_expired_activity(db: AsyncSession, *, now: Optional[datetime] = None) -> int:
    """Delete rows older than SEARCH_LOG_RETENTION_DAYS; the purge itself is audited."""
    days = search_log_retention_days()
    if days <= 0:
        return 0
    cutoff = (now or datetime.utcnow()) - timedelta(days=days)
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text("SET LOCAL bcp.search_log_purge = 'on'"))
    result = await db.execute(delete(SearchActivity).where(SearchActivity.created_at < cutoff))
    removed = int(result.rowcount or 0)
    if removed:
        await write_audit(
            db,
            username="system",
            action="search_log_purge",
            resource_type="search_activity",
            detail=f"removed={removed} older_than={cutoff.date().isoformat()} retention_days={days}",
            actor_type="system",
        )
    await db.commit()
    return removed
