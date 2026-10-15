"""Document tags: a controlled vocabulary (``tags``) and per-document assignments (``document_tags``).

``documents.keywords`` stays the denormalised list that search and the vector payload read;
``set_document_tags`` keeps the two in step.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DocumentRecord, DocumentTag, Tag

MAX_TAGS_PER_DOCUMENT = 24
TAG_SOURCES = ("ai", "uploader", "reviewer")
_BANGLA = re.compile(r"[\u0980-\u09FF]")
_NOT_SLUG = re.compile(r"[^\w\u0980-\u09FF\u200c\u200d]+", re.UNICODE)


def tag_slug(label: str) -> str:
    """Stable key for a label: NFC, case-folded, punctuation collapsed to single hyphens."""
    text = unicodedata.normalize("NFC", label or "").casefold()
    return _NOT_SLUG.sub("-", text).strip("-_")[:96]


def clean_labels(labels: Iterable[str]) -> List[str]:
    """Trimmed labels, de-duplicated by slug, in input order."""
    seen, out = set(), []
    for raw in labels or []:
        label = " ".join(str(raw or "").split())[:120]
        slug = tag_slug(label)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append(label)
    return out[:MAX_TAGS_PER_DOCUMENT]


def parse_tag_input(value: Optional[str]) -> List[str]:
    """Comma, semicolon or newline separated labels from a form field."""
    return clean_labels(re.split(r"[,;\n]", value or ""))


async def ensure_tags(db: AsyncSession, labels: Sequence[str], *, created_by: Optional[str] = None) -> List[Tag]:
    labels = clean_labels(labels)
    if not labels:
        return []
    slugs = [tag_slug(label) for label in labels]
    existing = {t.slug: t for t in (await db.execute(select(Tag).where(Tag.slug.in_(slugs)))).scalars()}
    out: List[Tag] = []
    for label, slug in zip(labels, slugs):
        tag = existing.get(slug)
        if tag is None:
            bangla = bool(_BANGLA.search(label))
            tag = Tag(slug=slug, label_en=label, label_bn=label if bangla else None, created_by=created_by)
            db.add(tag)
            existing[slug] = tag
        out.append(tag)
    await db.flush()
    return out


async def document_tags(db: AsyncSession, doc_id: str) -> List[Dict[str, object]]:
    rows = (
        await db.execute(
            select(Tag, DocumentTag)
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(DocumentTag.doc_id == doc_id)
            .order_by(DocumentTag.id)
        )
    ).all()
    return [
        {"slug": tag.slug, "label": tag.label_en, "source": link.source, "confirmed": bool(link.confirmed)}
        for tag, link in rows
    ]


async def set_document_tags(
    db: AsyncSession,
    document: DocumentRecord,
    labels: Sequence[str],
    *,
    source: str,
    confirmed: bool,
    username: Optional[str] = None,
) -> List[str]:
    """Replace the document's tags with ``labels`` and mirror them into ``documents.keywords``."""
    tags = await ensure_tags(db, labels, created_by=username)
    await db.execute(delete(DocumentTag).where(DocumentTag.doc_id == document.doc_id))
    for tag in tags:
        db.add(DocumentTag(doc_id=document.doc_id, tag_id=tag.id, source=source, confirmed=confirmed))
    document.keywords = [t.label_en for t in tags]
    await db.flush()
    return [t.slug for t in tags]


async def add_suggested_tags(db: AsyncSession, document: DocumentRecord, labels: Sequence[str]) -> List[str]:
    """Attach AI tags as unconfirmed, without touching tags a person already set."""
    tags = await ensure_tags(db, labels)
    have = set((await db.execute(select(DocumentTag.tag_id).where(DocumentTag.doc_id == document.doc_id))).scalars())
    for tag in tags:
        if tag.id not in have:
            db.add(DocumentTag(doc_id=document.doc_id, tag_id=tag.id, source="ai", confirmed=False))
    await db.flush()
    return [t.slug for t in tags]


async def confirm_document_tags(db: AsyncSession, doc_id: str) -> None:
    for link in (await db.execute(select(DocumentTag).where(DocumentTag.doc_id == doc_id))).scalars():
        link.confirmed = True


async def tag_slugs_for(db: AsyncSession, doc_id: str) -> List[str]:
    return list(
        (
            await db.execute(
                select(Tag.slug).join(DocumentTag, DocumentTag.tag_id == Tag.id).where(DocumentTag.doc_id == doc_id)
            )
        ).scalars()
    )


async def tag_facets(db: AsyncSession, *, limit: int = 200) -> List[Dict[str, object]]:
    """Most used active tags, for the search filter."""
    count = func.count(DocumentTag.id)
    rows = (
        await db.execute(
            select(Tag.slug, Tag.label_en, count)
            .join(DocumentTag, DocumentTag.tag_id == Tag.id)
            .where(Tag.is_active.is_(True))
            .group_by(Tag.slug, Tag.label_en)
            .order_by(count.desc(), Tag.label_en)
            .limit(limit)
        )
    ).all()
    return [{"slug": slug, "label": label, "count": int(n)} for slug, label, n in rows]


async def search_tags(db: AsyncSession, q: str, *, limit: int = 20) -> List[Dict[str, str]]:
    """Autocomplete over tag labels."""
    needle = " ".join((q or "").split())
    stmt = select(Tag.slug, Tag.label_en).where(Tag.is_active.is_(True))
    if needle:
        stmt = stmt.where(Tag.label_en.ilike(f"%{needle}%") | Tag.slug.ilike(f"%{tag_slug(needle)}%"))
    rows = (await db.execute(stmt.order_by(Tag.label_en).limit(limit))).all()
    return [{"slug": slug, "label": label} for slug, label in rows]
