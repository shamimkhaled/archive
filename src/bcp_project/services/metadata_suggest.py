"""Model-suggested document metadata. Suggestions are stored, never applied, until a person confirms."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger("bcp_project.services.metadata_suggest")

FIELDS = ("title", "reference_number", "department", "subject", "category", "doc_date", "doc_type", "keywords")
MAX_CHARS = 12000

SYSTEM = (
    "You index records for Sonali Bank PLC's archive. Read the document text (English, Bangla, or both) "
    "and return JSON with exactly these keys: title, reference_number, department, subject, category, "
    "doc_date (YYYY-MM-DD or empty), doc_type, keywords (list of up to 12 strings, English and Bangla). "
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

    sample = (text or "").strip()
    if not sample:
        return None
    sample = sample[:MAX_CHARS]
    try:
        payload = _clean(ai.chat_json("metadata", SYSTEM, sample, sensitivity=sensitivity, max_tokens=600))
        call = ai.last_call() or {}
        payload["_source"] = {"kind": "ai", "provider": call.get("provider"), "model": call.get("model")}
    except Exception as exc:
        logger.info("Metadata suggestion fell back to rules: %s", exc)
        payload = _clean({})
        payload["_source"] = {"kind": "rules", "reason": type(exc).__name__}
    if not payload.get("reference_number"):
        payload["reference_number"] = regex_reference(sample)
    return payload
