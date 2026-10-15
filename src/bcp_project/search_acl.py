"""Access control and metadata filters applied inside the vector query.

Every summary and chunk point carries ``allowed_roles`` (who may find it without a
grant), ``sensitivity``, and the confirmed metadata fields. A search builds one
Qdrant filter from the caller's scope and the requested filters, so restricted
vectors are never returned to someone who cannot open them. Postgres still makes
the final per-document decision (grants, verification) on the fused results.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AccessRequestStatus, DocumentAccessRequest, DocumentRecord, Role, Sensitivity

SERVICE_ROLE = "service"
ALL_ROLES = tuple(sorted([r.value for r in Role] + [SERVICE_ROLE]))
PRIVILEGED_ROLES = (Role.admin.value, Role.board_secretary.value)

# Summary points key on doc_id, chunk points on parent_doc_id.
SUMMARY_ID_FIELD = "doc_id"
CHUNK_ID_FIELD = "parent_doc_id"


def allowed_roles_for(sensitivity: Optional[str]) -> List[str]:
    if (sensitivity or Sensitivity.internal.value) == Sensitivity.restricted.value:
        return list(PRIVILEGED_ROLES)
    return list(ALL_ROLES)


def norm(value: Optional[str]) -> str:
    return " ".join((value or "").split()).casefold()


def day_number(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return None
    return value.year * 10000 + value.month * 100 + value.day


LANGUAGE_CODES = ("en", "bn", "mixed")
METADATA_CHECK_VALUES = ("checked", "unchecked")


def _tag_slugs(document: Any) -> List[str]:
    from .services.taxonomy import tag_slug

    slugs = []
    for keyword in getattr(document, "keywords", None) or []:
        slug = tag_slug(str(keyword))
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs


def search_payload(document: Any) -> Dict[str, Any]:
    """Fields stored on every vector of ``document`` (summary and chunks)."""
    sensitivity = getattr(document, "sensitivity", None) or Sensitivity.internal.value
    payload: Dict[str, Any] = {
        "sensitivity": sensitivity,
        "allowed_roles": allowed_roles_for(sensitivity),
        "doc_type_key": norm(getattr(document, "doc_type", None)),
        "department": norm(getattr(document, "department", None)) or None,
        "category": norm(getattr(document, "category", None)) or None,
        "reference_number": (getattr(document, "reference_number", None) or None),
        "doc_day": day_number(getattr(document, "doc_date", None)),
        "version": getattr(document, "current_version", None) or 1,
        "language": getattr(document, "language", None) or None,
        "tags": _tag_slugs(document) or None,
        "title_text": norm(getattr(document, "title", None)) or None,
        "subject_text": norm(getattr(document, "subject", None)) or None,
    }
    return {k: v for k, v in payload.items() if v is not None}


def payload_index_schema() -> Dict[str, Any]:
    from qdrant_client.http import models as rest

    keyword = rest.PayloadSchemaType.KEYWORD
    text = rest.PayloadSchemaType.TEXT
    return {
        "allowed_roles": keyword,
        "sensitivity": keyword,
        "doc_type_key": keyword,
        "department": keyword,
        "category": keyword,
        "doc_day": rest.PayloadSchemaType.INTEGER,
        "language": keyword,
        "tags": keyword,
        "title_text": text,
        "subject_text": text,
    }


@dataclass(frozen=True)
class SearchFilters:
    department: str = ""
    category: str = ""
    doc_type: str = ""
    reference_number: str = ""
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    subject: str = ""
    title: str = ""
    language: str = ""
    tags: Tuple[str, ...] = ()
    # "unchecked" | "checked": whether a person has confirmed AI-filled details. SQL only (not in Qdrant).
    metadata_check: str = ""

    @classmethod
    def from_params(
        cls,
        *,
        department: Optional[str] = None,
        category: Optional[str] = None,
        doc_type: Optional[str] = None,
        reference_number: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        subject: Optional[str] = None,
        title: Optional[str] = None,
        language: Optional[str] = None,
        tags: Optional[Union[str, Sequence[str]]] = None,
        metadata_check: Optional[str] = None,
    ) -> "SearchFilters":
        from .services.taxonomy import tag_slug
        def parse(value: Optional[str], name: str) -> Optional[date]:
            if not value:
                return None
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError as exc:
                raise ValueError(f"Invalid {name}, expected YYYY-MM-DD") from exc

        lang = (language or "").strip().lower()
        if lang and lang not in LANGUAGE_CODES:
            raise ValueError("Invalid language, expected en, bn or mixed")
        check = (metadata_check or "").strip().lower()
        if check and check not in METADATA_CHECK_VALUES:
            raise ValueError("Invalid metadata_check, expected checked or unchecked")
        slugs: List[str] = []
        raw_tags = tags.split(",") if isinstance(tags, str) else list(tags or [])
        for raw in raw_tags:
            slug = tag_slug(raw)
            if slug and slug not in slugs:
                slugs.append(slug)
        return cls(
            department=norm(department),
            category=norm(category),
            doc_type=norm(doc_type),
            reference_number=(reference_number or "").strip(),
            date_from=parse(date_from, "date_from"),
            date_to=parse(date_to, "date_to"),
            subject=norm(subject)[:200],
            title=norm(title)[:200],
            language=lang,
            tags=tuple(slugs[:10]),
            metadata_check=check,
        )

    @property
    def active(self) -> bool:
        return any(
            [
                self.department,
                self.category,
                self.doc_type,
                self.reference_number,
                self.date_from,
                self.date_to,
                self.subject,
                self.title,
                self.language,
                self.tags,
                self.metadata_check,
            ]
        )

    def as_dict(self) -> Dict[str, str]:
        return {
            "department": self.department,
            "category": self.category,
            "doc_type": self.doc_type,
            "reference_number": self.reference_number,
            "date_from": self.date_from.isoformat() if self.date_from else "",
            "date_to": self.date_to.isoformat() if self.date_to else "",
            "subject": self.subject,
            "title": self.title,
            "language": self.language,
            "tags": ",".join(self.tags),
            "metadata_check": self.metadata_check,
        }

    def cache_part(self) -> str:
        if not self.active:
            return ""
        raw = "|".join(f"{k}={v}" for k, v in sorted(self.as_dict().items()))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def qdrant_conditions(self) -> List[Any]:
        from qdrant_client.http import models as rest

        conditions: List[Any] = []
        for key, value in (("department", self.department), ("category", self.category), ("doc_type_key", self.doc_type)):
            if value:
                conditions.append(rest.FieldCondition(key=key, match=rest.MatchValue(value=value)))
        if self.reference_number:
            conditions.append(
                rest.FieldCondition(key="reference_number", match=rest.MatchValue(value=self.reference_number))
            )
        if self.date_from or self.date_to:
            conditions.append(
                rest.FieldCondition(
                    key="doc_day",
                    range=rest.Range(gte=day_number(self.date_from), lte=day_number(self.date_to)),
                )
            )
        if self.language:
            conditions.append(rest.FieldCondition(key="language", match=rest.MatchValue(value=self.language)))
        if self.tags:
            conditions.append(rest.FieldCondition(key="tags", match=rest.MatchAny(any=list(self.tags))))
        if self.title:
            conditions.append(rest.FieldCondition(key="title_text", match=rest.MatchText(text=self.title)))
        if self.subject:
            conditions.append(rest.FieldCondition(key="subject_text", match=rest.MatchText(text=self.subject)))
        return conditions

    def sql_conditions(self) -> List[Any]:
        from sqlalchemy import exists, func

        from .models import DocumentTag, Tag

        conditions: List[Any] = []
        if self.subject:
            conditions.append(func.lower(DocumentRecord.subject).contains(self.subject, autoescape=True))
        if self.title:
            conditions.append(func.lower(DocumentRecord.title).contains(self.title, autoescape=True))
        if self.language:
            conditions.append(DocumentRecord.language == self.language)
        if self.tags:
            conditions.append(
                exists(
                    select(DocumentTag.id)
                    .join(Tag, Tag.id == DocumentTag.tag_id)
                    .where(DocumentTag.doc_id == DocumentRecord.doc_id, Tag.slug.in_(self.tags))
                )
            )
        if self.department:
            conditions.append(func.lower(DocumentRecord.department) == self.department)
        if self.category:
            conditions.append(func.lower(DocumentRecord.category) == self.category)
        if self.doc_type:
            conditions.append(func.lower(DocumentRecord.doc_type) == self.doc_type)
        if self.reference_number:
            conditions.append(DocumentRecord.reference_number == self.reference_number)
        if self.date_from:
            conditions.append(DocumentRecord.doc_date >= self.date_from)
        if self.date_to:
            conditions.append(DocumentRecord.doc_date <= self.date_to)
        if self.metadata_check:
            conditions.append(DocumentRecord.metadata_check_pending.is_(self.metadata_check == "unchecked"))
        return conditions


@dataclass(frozen=True)
class AclScope:
    """What the caller may find. ``unrestricted`` skips the vector ACL filter entirely."""

    unrestricted: bool
    role: str = SERVICE_ROLE
    granted_ids: Tuple[str, ...] = field(default_factory=tuple)

    def cache_part(self) -> str:
        if self.unrestricted:
            return "all"
        if not self.granted_ids:
            return self.role
        digest = hashlib.sha256(",".join(sorted(self.granted_ids)).encode()).hexdigest()[:16]
        return f"{self.role}:{digest}"

    def qdrant_conditions(self, id_field: str) -> List[Any]:
        """``should`` clauses: role may see it, or the caller holds a grant for that document."""
        if self.unrestricted:
            return []
        from qdrant_client.http import models as rest

        should: List[Any] = [rest.FieldCondition(key="allowed_roles", match=rest.MatchAny(any=[self.role]))]
        if self.granted_ids:
            should.append(rest.FieldCondition(key=id_field, match=rest.MatchAny(any=list(self.granted_ids))))
        return should

    def sql_condition(self) -> Optional[Any]:
        if self.unrestricted:
            return None
        visible = DocumentRecord.sensitivity != Sensitivity.restricted.value
        if self.granted_ids:
            return or_(visible, DocumentRecord.doc_id.in_(self.granted_ids))
        return visible


def build_filter(acl: AclScope, filters: Optional[SearchFilters], id_field: str, *, doc_ids: Optional[Iterable[str]] = None):
    from qdrant_client.http import models as rest

    must = list(filters.qdrant_conditions()) if filters else []
    if doc_ids is not None:
        must.append(rest.FieldCondition(key=id_field, match=rest.MatchAny(any=list(doc_ids))))
    should = acl.qdrant_conditions(id_field)
    if not must and not should:
        return None
    return rest.Filter(must=must or None, should=should or None)


async def acl_scope_for_user(db: AsyncSession, user: Any) -> AclScope:
    role = getattr(getattr(user, "role", None), "value", getattr(user, "role", None)) or SERVICE_ROLE
    if role in PRIVILEGED_ROLES:
        return AclScope(unrestricted=True, role=role)
    now = datetime.utcnow()
    rows = await db.execute(
        select(DocumentAccessRequest.doc_id).where(
            and_(
                DocumentAccessRequest.requester_username == user.username,
                DocumentAccessRequest.status == AccessRequestStatus.approved,
                or_(DocumentAccessRequest.expires_at.is_(None), DocumentAccessRequest.expires_at > now),
            )
        )
    )
    granted = tuple(sorted({r[0] for r in rows.all() if r[0]}))
    return AclScope(unrestricted=False, role=role, granted_ids=granted)
