"""Human correction effort observed in the audit log, rather than estimated from edit distance.

OCR verification: time from ``verify_open`` to the same reviewer's ``verify_approve`` or
``verify_reject`` for that document version, pages edited, and metadata fields changed.
Minutes and briefings: number of saves or section edits before approval and the elapsed time.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

MAX_SESSION_MINUTES = 120.0
_VERSION = re.compile(r"\bv(\d+)\b")
_PAGES = re.compile(r"pages_edited=(\d+)")
_CHANGED = re.compile(r"changed=([\w,]+)")


def _version(detail: str) -> Optional[str]:
    match = _VERSION.search(detail or "")
    return match.group(1) if match else None


def _summary(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"n": 0, "median": None, "p90": None, "mean": None}
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
    return {"n": len(values), "median": round(statistics.median(ordered), 2), "p90": round(p90, 2), "mean": round(statistics.mean(ordered), 2)}


def correction_effort(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    events = sorted(rows, key=lambda r: (r["created_at"], r.get("id") or 0))
    open_at: Dict[tuple, datetime] = {}
    review_minutes: List[float] = []
    pages_edited: List[int] = []
    approved = rejected = abandoned = 0
    fields_changed: Dict[str, int] = {}

    minutes_saves: Dict[str, int] = {}
    minutes_first: Dict[str, datetime] = {}
    minutes_rows: List[Dict[str, Any]] = []
    brief_edits: Dict[str, int] = {}
    brief_started: Dict[str, datetime] = {}
    brief_rows: List[Dict[str, Any]] = []

    for row in events:
        action = row.get("action")
        resource = str(row.get("resource_id") or "")
        detail = row.get("detail") or ""
        at: datetime = row["created_at"]
        user = row.get("username")

        if action == "verify_open":
            open_at[(user, resource, _version(detail))] = at
        elif action in ("verify_approve", "verify_reject"):
            started = open_at.pop((user, resource, _version(detail)), None)
            if started is not None:
                minutes = (at - started).total_seconds() / 60
                if 0 <= minutes <= MAX_SESSION_MINUTES:
                    review_minutes.append(minutes)
                else:
                    abandoned += 1
            if action == "verify_approve":
                approved += 1
                match = _PAGES.search(detail)
                if match:
                    pages_edited.append(int(match.group(1)))
            else:
                rejected += 1
        elif action == "metadata_confirm":
            match = _CHANGED.search(detail)
            for name in (match.group(1).split(",") if match else []):
                if name and name != "none":
                    fields_changed[name] = fields_changed.get(name, 0) + 1

        elif action == "minutes_save":
            minutes_saves[resource] = minutes_saves.get(resource, 0) + 1
            minutes_first.setdefault(resource, at)
        elif action == "minutes_approve":
            first = minutes_first.pop(resource, None)
            minutes_rows.append(
                {
                    "meeting_id": resource,
                    "saves_before_approval": minutes_saves.pop(resource, 0),
                    "hours_to_approval": round((at - first).total_seconds() / 3600, 2) if first else None,
                }
            )

        elif action == "briefing_generate":
            brief_started.setdefault(resource, at)
        elif action == "briefing_edit":
            brief_edits[resource] = brief_edits.get(resource, 0) + 1
        elif action == "briefing_approve":
            started = brief_started.pop(resource, None)
            brief_rows.append(
                {
                    "doc_id": resource,
                    "section_edits": brief_edits.pop(resource, 0),
                    "hours_to_approval": round((at - started).total_seconds() / 3600, 2) if started else None,
                }
            )

    return {
        "ocr_verification": {
            "approved": approved,
            "rejected": rejected,
            "sessions_over_cap_or_unpaired": abandoned,
            "minutes_per_document": _summary(review_minutes),
            "pages_edited_per_document": _summary([float(p) for p in pages_edited]),
            "metadata_fields_corrected": dict(sorted(fields_changed.items(), key=lambda kv: -kv[1])),
        },
        "minutes": {
            "approved": len(minutes_rows),
            "saves_before_approval": _summary([float(r["saves_before_approval"]) for r in minutes_rows]),
            "hours_to_approval": _summary([r["hours_to_approval"] for r in minutes_rows if r["hours_to_approval"] is not None]),
        },
        "briefings": {
            "approved": len(brief_rows),
            "section_edits_before_approval": _summary([float(r["section_edits"]) for r in brief_rows]),
            "hours_to_approval": _summary([r["hours_to_approval"] for r in brief_rows if r["hours_to_approval"] is not None]),
        },
    }
