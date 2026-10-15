"""Board paper packages: a memo plus its annexures and supporting papers, briefed as one item.

Members are labelled by role and order (M, A1, A2, S1 ...). Any change to membership bumps the
package revision; a briefing records the revision and a snapshot of member versions, so a new
approved version of any member also marks the briefing stale.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import BoardBriefing, BoardMeeting, BoardPaperPackage, BoardPaperPackageItem, DocumentRecord, MeetingDocument
from .briefing import BriefingWorkflowError, current_version_number

ROLE_MEMO = "memo"
ROLE_ANNEXURE = "annexure"
ROLE_SUPPORTING = "supporting"
ROLES = (ROLE_MEMO, ROLE_ANNEXURE, ROLE_SUPPORTING)
ROLE_LABELS = {ROLE_MEMO: "Memo", ROLE_ANNEXURE: "Annexure", ROLE_SUPPORTING: "Supporting paper"}
MAX_MEMBERS = 20


async def get_package(db: AsyncSession, package_id: int, *, for_update: bool = False) -> Optional[BoardPaperPackage]:
    stmt = select(BoardPaperPackage).where(BoardPaperPackage.id == package_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def package_items(db: AsyncSession, package_id: int) -> List[BoardPaperPackageItem]:
    rows = await db.execute(
        select(BoardPaperPackageItem)
        .where(BoardPaperPackageItem.package_id == package_id)
        .order_by(BoardPaperPackageItem.seq, BoardPaperPackageItem.id)
    )
    return list(rows.scalars().all())


def _ordered(items: Sequence[BoardPaperPackageItem]) -> List[BoardPaperPackageItem]:
    rank = {ROLE_MEMO: 0, ROLE_ANNEXURE: 1, ROLE_SUPPORTING: 2}
    return sorted(items, key=lambda i: (rank.get(i.role, 3), i.seq, i.id or 0))


def relabel(items: Sequence[BoardPaperPackageItem]) -> List[BoardPaperPackageItem]:
    """Memo first, then annexures A1.., then supporting papers S1..; seq follows that order."""
    counters = {ROLE_ANNEXURE: 0, ROLE_SUPPORTING: 0}
    ordered = _ordered(items)
    for seq, item in enumerate(ordered):
        item.seq = seq
        if item.role == ROLE_MEMO:
            item.label = "M"
        else:
            counters[item.role] = counters.get(item.role, 0) + 1
            item.label = ("A" if item.role == ROLE_ANNEXURE else "S") + str(counters[item.role])
    return ordered


def _touch(package: BoardPaperPackage) -> None:
    package.revision = int(package.revision or 1) + 1
    package.updated_at = datetime.utcnow()


async def create_package(
    db: AsyncSession, *, title: str, username: str, meeting_id: Optional[int] = None, memo_doc_id: str = ""
) -> BoardPaperPackage:
    title = " ".join((title or "").split())[:255]
    if not title:
        raise BriefingWorkflowError("Give the package a title.", status_code=400)
    if meeting_id is not None and await db.get(BoardMeeting, meeting_id) is None:
        raise BriefingWorkflowError("That meeting does not exist.", status_code=400)
    package = BoardPaperPackage(title=title, meeting_id=meeting_id, created_by=username, revision=1)
    db.add(package)
    await db.flush()
    if memo_doc_id.strip():
        await add_member(db, package, memo_doc_id, ROLE_MEMO, username)
        package.revision = 1
    return package


async def add_member(
    db: AsyncSession, package: BoardPaperPackage, doc_id: str, role: str, username: str
) -> BoardPaperPackageItem:
    doc_id = (doc_id or "").strip()
    role = role if role in ROLES else ROLE_ANNEXURE
    document = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == doc_id))).scalar_one_or_none()
    if document is None:
        raise BriefingWorkflowError(f"No paper with archive number {doc_id or '(blank)'}.", status_code=400)
    items = await package_items(db, package.id)
    if any(i.doc_id == doc_id for i in items):
        raise BriefingWorkflowError(f"{doc_id} is already in this package.", status_code=400)
    if len(items) >= MAX_MEMBERS:
        raise BriefingWorkflowError(f"A package can hold at most {MAX_MEMBERS} papers.", status_code=400)
    if role == ROLE_MEMO and any(i.role == ROLE_MEMO for i in items):
        raise BriefingWorkflowError("This package already has a memo. Remove it first.", status_code=400)
    item = BoardPaperPackageItem(
        package_id=package.id, doc_id=doc_id, role=role, label="", seq=len(items) + 100, added_by=username
    )
    db.add(item)
    relabel([*items, item])
    if role == ROLE_MEMO:
        package.memo_doc_id = doc_id
    _touch(package)
    await db.flush()
    return item


async def remove_member(db: AsyncSession, package: BoardPaperPackage, doc_id: str) -> None:
    items = await package_items(db, package.id)
    target = next((i for i in items if i.doc_id == doc_id), None)
    if target is None:
        raise BriefingWorkflowError("That paper is not in this package.", status_code=400)
    await db.delete(target)
    if target.role == ROLE_MEMO:
        package.memo_doc_id = None
    relabel([i for i in items if i is not target])
    _touch(package)
    await db.flush()


async def move_member(db: AsyncSession, package: BoardPaperPackage, doc_id: str, direction: str) -> None:
    """Swap with the neighbour of the same role; the memo always stays first."""
    items = _ordered(await package_items(db, package.id))
    index = next((n for n, i in enumerate(items) if i.doc_id == doc_id), None)
    if index is None:
        raise BriefingWorkflowError("That paper is not in this package.", status_code=400)
    other = index - 1 if direction == "up" else index + 1
    if other < 0 or other >= len(items) or items[other].role != items[index].role:
        return
    items[index].seq, items[other].seq = items[other].seq, items[index].seq
    relabel(items)
    _touch(package)
    await db.flush()


async def member_documents(db: AsyncSession, items: Sequence[BoardPaperPackageItem]) -> Dict[str, DocumentRecord]:
    ids = [i.doc_id for i in items]
    if not ids:
        return {}
    rows = await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id.in_(ids)))
    return {d.doc_id: d for d in rows.scalars().all()}


async def package_snapshot(db: AsyncSession, package: BoardPaperPackage) -> List[Dict[str, Any]]:
    """Members with the approved version each would be briefed at."""
    items = _ordered(await package_items(db, package.id))
    docs = await member_documents(db, items)
    out = []
    for item in items:
        document = docs.get(item.doc_id)
        if document is None:
            continue
        out.append(
            {
                "doc_id": item.doc_id,
                "version": await current_version_number(db, document),
                "role": item.role,
                "label": item.label,
                "seq": item.seq,
                "page_count": document.page_count,
                "title": document.title or item.doc_id,
            }
        )
    return out


def snapshot_key(members: Optional[Sequence[Dict[str, Any]]]) -> Tuple[Tuple[str, int, str], ...]:
    return tuple((str(m.get("doc_id")), int(m.get("version") or 1), str(m.get("label") or "")) for m in members or [])


def is_stale(briefing: Optional[BoardBriefing], package: BoardPaperPackage, snapshot: Sequence[Dict[str, Any]]) -> bool:
    if briefing is None:
        return False
    return briefing.package_revision != package.revision or snapshot_key(briefing.members) != snapshot_key(snapshot)


async def prepare_revision(
    db: AsyncSession, package: BoardPaperPackage, snapshot: Sequence[Dict[str, Any]], existing: Optional[BoardBriefing]
) -> None:
    """A member's new approved version needs a new revision once the old briefing left draft."""
    if existing is None or snapshot_key(existing.members) == snapshot_key(snapshot):
        return
    if (existing.state or "ai_draft") != "ai_draft":
        _touch(package)
        await db.flush()


async def packages_for_meeting(db: AsyncSession, meeting_id: int) -> List[BoardPaperPackage]:
    rows = await db.execute(
        select(BoardPaperPackage).where(BoardPaperPackage.meeting_id == meeting_id).order_by(BoardPaperPackage.id)
    )
    return list(rows.scalars().all())


async def meeting_packages_view(db: AsyncSession, meeting_id: int, *, approved_only: bool = False) -> List[Dict[str, Any]]:
    """Packages linked to a meeting with their latest briefing state; readers see approved ones only."""
    out = []
    for package in await packages_for_meeting(db, meeting_id):
        states = (
            await db.execute(
                select(BoardBriefing.state)
                .where(BoardBriefing.package_id == package.id)
                .order_by(BoardBriefing.package_revision.desc())
            )
        ).scalars().all()
        approved = "approved" in states
        if approved_only and not approved:
            continue
        count = (
            await db.execute(
                select(func.count(BoardPaperPackageItem.id)).where(BoardPaperPackageItem.package_id == package.id)
            )
        ).scalar() or 0
        out.append(
            {
                "id": package.id,
                "title": package.title,
                "members": int(count),
                "state": states[0] if states else None,
                "approved": approved,
            }
        )
    return out


async def list_packages(db: AsyncSession, *, limit: int = 200) -> List[Dict[str, Any]]:
    packages = (
        await db.execute(select(BoardPaperPackage).order_by(BoardPaperPackage.id.desc()).limit(limit))
    ).scalars().all()
    if not packages:
        return []
    ids = [p.id for p in packages]
    counts = dict(
        (
            await db.execute(
                select(BoardPaperPackageItem.package_id, func.count(BoardPaperPackageItem.id))
                .where(BoardPaperPackageItem.package_id.in_(ids))
                .group_by(BoardPaperPackageItem.package_id)
            )
        ).all()
    )
    meeting_ids = {p.meeting_id for p in packages if p.meeting_id}
    meetings = {}
    if meeting_ids:
        meetings = {
            m.id: m for m in (await db.execute(select(BoardMeeting).where(BoardMeeting.id.in_(meeting_ids)))).scalars().all()
        }
    states = {}
    for row in (
        await db.execute(
            select(BoardBriefing.package_id, BoardBriefing.package_revision, BoardBriefing.state, BoardBriefing.status)
            .where(BoardBriefing.package_id.in_(ids))
            .order_by(BoardBriefing.package_revision)
        )
    ).all():
        states[row[0]] = {"revision": row[1], "state": row[2], "status": row[3]}
    return [
        {
            "package": p,
            "members": counts.get(p.id, 0),
            "meeting": meetings.get(p.meeting_id),
            "briefing": states.get(p.id),
        }
        for p in packages
    ]


async def meeting_pack_candidates(db: AsyncSession, package: BoardPaperPackage) -> List[Dict[str, Any]]:
    """Archived papers in the linked meeting's pack that are not in the package yet."""
    if not package.meeting_id:
        return []
    member_ids = {i.doc_id for i in await package_items(db, package.id)}
    rows = await db.execute(
        select(MeetingDocument.archive_doc_id, MeetingDocument.title)
        .where(MeetingDocument.meeting_id == package.meeting_id, MeetingDocument.archive_doc_id.isnot(None))
        .order_by(MeetingDocument.id)
    )
    return [{"doc_id": d, "title": t} for d, t in rows.all() if d and d not in member_ids]


def _reference_prefix(reference: Optional[str]) -> str:
    ref = (reference or "").strip()
    for sep in ("/", "-"):
        if sep in ref:
            return ref.rsplit(sep, 1)[0] + sep
    return ""


async def suggest_annexures(db: AsyncSession, package: BoardPaperPackage, *, limit: int = 5) -> List[DocumentRecord]:
    """Papers whose reference number shares the memo's prefix (e.g. SB/HO/2026/)."""
    if not package.memo_doc_id:
        return []
    memo = (
        await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == package.memo_doc_id))
    ).scalar_one_or_none()
    prefix = _reference_prefix(memo.reference_number if memo else None)
    if not prefix:
        return []
    member_ids = {i.doc_id for i in await package_items(db, package.id)}
    rows = await db.execute(
        select(DocumentRecord)
        .where(DocumentRecord.reference_number.startswith(prefix, autoescape=True))
        .order_by(DocumentRecord.doc_date.desc())
        .limit(limit + len(member_ids))
    )
    return [d for d in rows.scalars().all() if d.doc_id not in member_ids][:limit]
