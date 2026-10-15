"""Model-suggested document metadata.

Suggestions fill only fields the uploader left empty and stay marked unconfirmed until a
person confirms them. Weak extractions (no model, missing title, type or date disagreeing
with the uploader) send the version to the verification queue instead.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from datetime import date
from typing import Any, Dict, List, Optional

from ..document_types import DEFAULT_DOCUMENT_TYPES, normalize_document_type
from ..ui import reason_label

logger = logging.getLogger("bcp_project.services.metadata_suggest")

FIELDS = ("title", "reference_number", "department", "subject", "category", "doc_date", "doc_type", "keywords", "language")
APPLY_FIELDS = ("title", "reference_number", "department", "subject", "category")
# Fields whose confidence can send a version to review; keywords and language never block on their own.
SCORED_FIELDS = ("title", "reference_number", "department", "subject", "category", "doc_date", "doc_type")
VERBATIM_FIELDS = ("title", "reference_number", "subject")
LANGUAGES = ("en", "bn", "mixed")
MAX_CHARS = 12000
UNCLASSIFIED_TYPES = {"", "other"}

SYSTEM = (
    "You index records for Sonali Bank PLC's archive. Read the document text (English, Bangla, or both) "
    "and return JSON with exactly these keys: title, reference_number, department, subject, category, "
    "doc_date (YYYY-MM-DD or empty), doc_type, keywords (list of up to 12 strings, English and Bangla), "
    "language (en, bn, or mixed), confidence (object mapping each of title, reference_number, department, "
    "subject, category, doc_date, doc_type to a number from 0 to 1: how sure you are the text states it). "
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
        elif key == "language":
            lang = str(value or "").strip().lower()
            out[key] = lang if lang in LANGUAGES else ""
        else:
            out[key] = str(value or "").strip()[:300]
    raw = payload.get("confidence")
    if isinstance(raw, dict):
        scores = {}
        for key in SCORED_FIELDS:
            try:
                scores[key] = max(0.0, min(1.0, float(raw.get(key))))
            except (TypeError, ValueError):
                continue
        if scores:
            out["_model_confidence"] = scores
    return out


_BANGLA_LETTER = re.compile(r"[\u0980-\u09FF]")


def detect_language(text: str) -> Optional[str]:
    """``bn`` or ``en`` when at least 85% of letters are that script, ``mixed`` otherwise; None without letters."""
    letters = [c for c in (text or "") if c.isalpha()]
    if len(letters) < 20:
        return None
    bangla = sum(1 for c in letters if _BANGLA_LETTER.match(c)) / len(letters)
    if bangla >= 0.85:
        return "bn"
    if bangla <= 0.15:
        return "en"
    return "mixed"


def _flat(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text or "").split()).casefold()


def _date_on_page(value: date, flat: str) -> bool:
    forms = {
        value.isoformat(),
        value.strftime("%d/%m/%Y"),
        value.strftime("%d-%m-%Y"),
        value.strftime("%d.%m.%Y"),
        f"{value.day} {value.strftime('%B')} {value.year}".casefold(),
        f"{value.strftime('%B')} {value.day}, {value.year}".casefold(),
        f"{value.day:02d} {value.strftime('%B')} {value.year}".casefold(),
    }
    return any(form.casefold() in flat for form in forms)


def field_min_confidence() -> float:
    try:
        return float(os.getenv("METADATA_FIELD_MIN_CONFIDENCE") or 0.7)
    except ValueError:
        return 0.7


def score_fields(
    text: str, suggested: Optional[Dict[str, Any]], document: Any, ocr_mean: Optional[float] = None
) -> Dict[str, float]:
    """Confidence per metadata field for the value the archive will keep.

    A value a person entered scores 1.0. A suggested value starts from the model's own score (0.75
    when it gave none, 0.5 for the rules fallback), gains when it is found verbatim in the text and
    loses when a value that must be copied exactly is not, then is scaled by OCR quality.
    """
    suggested = suggested or {}
    source = (suggested.get("_source") or {}).get("kind")
    model = suggested.get("_model_confidence") or {}
    flat = _flat(text)
    ocr_factor = 1.0 if ocr_mean is None else 0.7 + 0.3 * max(0.0, min(1.0, float(ocr_mean) / 100.0))
    scores: Dict[str, float] = {}
    for field in SCORED_FIELDS:
        entered = getattr(document, field, None)
        if field == "doc_type" and normalize_document_type(entered or "").casefold() in UNCLASSIFIED_TYPES:
            entered = None
        value = suggested.get(field)
        if entered and field != "doc_date":
            scores[field] = 1.0
            continue
        if field == "doc_date":
            found = _parse_date(value)
            if entered and (not found or found == entered):
                scores[field] = 1.0
                continue
            if found is None:
                continue
            score = float(model.get(field, 0.75 if source == "ai" else 0.5))
            score += 0.15 if _date_on_page(found, flat) else -0.1
            if entered and found != entered:
                score = min(score, 0.3)
            scores[field] = round(max(0.0, min(1.0, score * ocr_factor)), 2)
            continue
        if not value:
            continue
        score = float(model.get(field, 0.75 if source == "ai" else 0.5))
        if field == "reference_number" and value == regex_reference(text):
            score = max(score, 0.95)
        elif field in VERBATIM_FIELDS:
            if _flat(value) in flat:
                score += 0.15
            elif field == "reference_number":
                score = min(score, 0.4)
            else:
                score -= 0.1
        scores[field] = round(max(0.0, min(1.0, score * ocr_factor)), 2)
    language = getattr(document, "language", None)
    if language:
        scores["language"] = 1.0
    elif detect_language(text):
        scores["language"] = 0.95
    return scores


def low_confidence_fields(confidence: Optional[Dict[str, Any]], threshold: Optional[float] = None) -> List[str]:
    limit = field_min_confidence() if threshold is None else threshold
    out = []
    for field in SCORED_FIELDS:
        value = (confidence or {}).get(field)
        if isinstance(value, (int, float)) and value < limit:
            out.append(field)
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
    flagged = {"title": "missing_title", "doc_type": "doc_type_mismatch", "doc_date": "doc_date_mismatch"}
    low = [f for f in low_confidence_fields(getattr(document, "metadata_confidence", None)) if flagged.get(f) not in reasons]
    if low:
        reasons.append("low_metadata_confidence")
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
    language = suggested.get("language")
    if language in LANGUAGES and not getattr(document, "language", None):
        document.language = language
        applied.append("language")
    return applied


REASON_CODES = (
    "low_ocr_confidence",
    "bangla_ocr_suspect",
    "ai_unavailable",
    "missing_title",
    "doc_type_mismatch",
    "doc_date_mismatch",
    "low_metadata_confidence",
)
REASON_LABELS = {code: reason_label(code) for code in REASON_CODES}
