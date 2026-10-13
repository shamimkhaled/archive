"""Model-suggested document metadata.

Suggestions fill only fields the uploader left empty and stay marked unconfirmed until a
person confirms them. Weak extractions (no model, missing title, type or date disagreeing
with the uploader) send the version to the verification queue instead.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Any, Dict, List, Optional

from ..document_types import DEFAULT_DOCUMENT_TYPES, normalize_document_type

logger = logging.getLogger("bcp_project.services.metadata_suggest")

FIELDS = ("title", "reference_number", "department", "subject", "category", "doc_date", "doc_type", "keywords")
APPLY_FIELDS = ("title", "reference_number", "department", "subject", "category")
MAX_CHARS = 12000
UNCLASSIFIED_TYPES = {"", "other"}

SYSTEM = (
    "You index records for Sonali Bank PLC's archive. Read the document text (English, Bangla, or both) "
    "and return JSON with exactly these keys: title, reference_number, department, subject, category, "
    "doc_date (YYYY-MM-DD or empty), doc_type, keywords (list of up to 12 strings, English and Bangla). "
    f"doc_type must be one of: {', '.join(DEFAULT_DOCUMENT_TYPES)}. "
    "Use empty strings when the text does not state a value. Copy reference numbers exactly as written. "
    "Do not guess."
)

_REF = re.compile(
    r"(?:(?:Ref(?:erence)?|Memo|Circular|সূত্র|স্মারক)\s*(?:No\.?|নং)?\s*[:：]?\s*)([A-Za-z0-9\u0980-\u09FF][A-Za-z0-9\u0980-\u09FF/().\-]{3,80})",
    re.IGNORECASE,
)


def regex_reference(text: str) -> str:
    match = _REF.search(text or "")
    return match.group(1).strip(" .,;") if match else ""


def _clean(payload: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in FIELDS:
        value = payload.get(key)
        if key == "keywords":
            out[key] = [str(v).strip() for v in (value or []) if str(v).strip()][:12] if isinstance(value, list) else []
        else:
            out[key] = str(value or "").strip()[:300]
    return out


def suggest_metadata(text: str, *, sensitivity: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return suggested fields plus ``_source``; None when there is no text."""
    from .. import ai
    from ..ai.guard import with_rules, wrap_untrusted

    sample = (text or "").strip()
    if not sample:
        return None
    sample = sample[:MAX_CHARS]
    try:
        payload = _clean(
            ai.chat_json(
                "metadata", with_rules(SYSTEM), wrap_untrusted(sample, "document"), sensitivity=sensitivity, max_tokens=600
            )
        )
        call = ai.last_call() or {}
        payload["_source"] = {"kind": "ai", "provider": call.get("provider"), "model": call.get("model")}
    except Exception as exc:
        logger.info("Metadata suggestion fell back to rules: %s", exc)
        payload = _clean({})
        payload["_source"] = {"kind": "rules", "reason": type(exc).__name__}
    if not payload.get("reference_number"):
        payload["reference_number"] = regex_reference(sample)
    return payload


def metadata_review_enabled() -> bool:
    return (os.getenv("METADATA_REVIEW") or "on").strip().lower() not in {"0", "off", "false", "no"}


def _parse_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def metadata_review_reasons(document: Any, suggested: Optional[Dict[str, Any]]) -> List[str]:
    """Why the extracted metadata needs a person, or an empty list when it can be trusted as a draft.

    A rules fallback caused by the egress policy is expected for restricted papers, so it only
    counts when the uploader also left the title empty.
    """
    if not metadata_review_enabled():
        return []
    suggested = suggested or {}
    source = suggested.get("_source") or {}
    reasons: List[str] = []
    if source.get("kind") == "rules" and source.get("reason") != "EgressBlocked":
        reasons.append("ai_unavailable")
    if not (getattr(document, "title", None) or suggested.get("title")):
        reasons.append("missing_title")

    chosen = normalize_document_type(getattr(document, "doc_type", "") or "").casefold()
    proposed = normalize_document_type(suggested.get("doc_type") or "").casefold()
    if proposed and proposed not in UNCLASSIFIED_TYPES and chosen not in UNCLASSIFIED_TYPES and proposed != chosen:
        reasons.append("doc_type_mismatch")

    found = _parse_date(suggested.get("doc_date"))
    entered = getattr(document, "doc_date", None)
    if found and entered and found != entered:
        reasons.append("doc_date_mismatch")
    return reasons


def apply_suggestions(document: Any, suggested: Optional[Dict[str, Any]]) -> List[str]:
    """Fill empty fields from the suggestion (never overwrite the uploader). Returns the fields filled."""
    suggested = suggested or {}
    applied: List[str] = []
    for field in APPLY_FIELDS:
        value = str(suggested.get(field) or "").strip()
        if value and not getattr(document, field, None):
            setattr(document, field, value)
            applied.append(field)
    proposed = normalize_document_type(suggested.get("doc_type") or "")
    if proposed and normalize_document_type(document.doc_type or "").casefold() in UNCLASSIFIED_TYPES:
        document.doc_type = proposed
        applied.append("doc_type")
    keywords = [k for k in suggested.get("keywords") or [] if k]
    if keywords and not document.keywords:
        document.keywords = keywords
        applied.append("keywords")
    return applied


REASON_LABELS = {
    "low_ocr_confidence": "Low OCR confidence",
    "bangla_ocr_suspect": "Bangla words misread as Latin characters",
    "ai_unavailable": "AI metadata extraction unavailable",
    "missing_title": "No title found",
    "doc_type_mismatch": "AI classification differs from the uploader's type",
    "doc_date_mismatch": "Date in the text differs from the entered date",
}
