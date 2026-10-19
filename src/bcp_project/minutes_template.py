"""Configurable organisational minutes PDF chrome (requirement 3.2).

Env (all optional):
  MINUTES_TEMPLATE_LANG   en | bn | both   (default: both)
  MINUTES_LOGO_PATH       filesystem path or absolute URL path under static/
  MINUTES_HEADER_EN       override English header line
  MINUTES_HEADER_BN       override Bangla header line
  MINUTES_DOC_NUMBER_FMT  e.g. ``MIN-{meeting_id}`` (placeholders: meeting_id, version, year)
  MINUTES_SIGNATURE_EN    comma-separated role labels for the signature block (English)
  MINUTES_SIGNATURE_BN    comma-separated role labels (Bangla); used when lang is bn/both
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from .brand import BRAND

_PACKAGE_ROOT = Path(__file__).resolve().parent
_DEFAULT_LOGO = _PACKAGE_ROOT / "static" / "img" / "sonali-bank-logo.png"

_DEFAULT_SIG_EN = ("Chairperson", "Member Secretary", "Managing Director")
_DEFAULT_SIG_BN = ("চেয়ারপারসন", "সদস্য সচিব", "ম্যানেজিং ডিরেক্টর")


@dataclass(frozen=True)
class MinutesTemplate:
    """Bank letterhead settings for minutes PDFs."""

    lang: str  # en | bn | both
    org_en: str
    org_bn: str
    header_en: str
    header_bn: str
    logo_path: Optional[Path]
    signature_en: tuple[str, ...]
    signature_bn: tuple[str, ...]
    show_logo: bool
    show_signatures: bool

    @property
    def bilingual(self) -> bool:
        return self.lang == "both"

    @property
    def prefer_bn(self) -> bool:
        return self.lang == "bn"


def _split_labels(raw: str, fallback: Sequence[str]) -> tuple[str, ...]:
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    return tuple(parts) if parts else tuple(fallback)


def resolve_logo_path(configured: Optional[str] = None) -> Optional[Path]:
    """Resolve MINUTES_LOGO_PATH or the packaged Sonali logo to a readable file."""
    candidates: List[Path] = []
    raw = (configured if configured is not None else os.getenv("MINUTES_LOGO_PATH") or "").strip()
    if raw:
        if raw.startswith("/static/"):
            candidates.append(_PACKAGE_ROOT / raw.lstrip("/"))
            # brand.logo_path style
            candidates.append(_PACKAGE_ROOT / "static" / raw[len("/static/") :])
        candidates.append(Path(raw))
    brand_logo = (BRAND.logo_path or "").strip()
    if brand_logo.startswith("/static/"):
        candidates.append(_PACKAGE_ROOT / brand_logo.lstrip("/"))
    candidates.append(_DEFAULT_LOGO)
    for path in candidates:
        try:
            if path.is_file():
                return path.resolve()
        except OSError:
            continue
    return None


def load_minutes_template() -> MinutesTemplate:
    lang = (os.getenv("MINUTES_TEMPLATE_LANG") or "both").strip().lower()
    if lang not in {"en", "bn", "both"}:
        lang = "both"
    logo = resolve_logo_path()
    show_logo = (os.getenv("MINUTES_SHOW_LOGO") or "1").strip().lower() not in {"0", "false", "no", "off"}
    show_sig = (os.getenv("MINUTES_SHOW_SIGNATURES") or "1").strip().lower() not in {"0", "false", "no", "off"}
    return MinutesTemplate(
        lang=lang,
        org_en=BRAND.org_name,
        org_bn=BRAND.org_name_bn,
        header_en=(os.getenv("MINUTES_HEADER_EN") or "").strip()
        or f"{BRAND.org_name} — Board Meeting Minutes",
        header_bn=(os.getenv("MINUTES_HEADER_BN") or "").strip()
        or f"{BRAND.org_name_bn} — বোর্ড সভার কার্যবিবরণী",
        logo_path=logo if show_logo else None,
        signature_en=_split_labels(os.getenv("MINUTES_SIGNATURE_EN") or "", _DEFAULT_SIG_EN),
        signature_bn=_split_labels(os.getenv("MINUTES_SIGNATURE_BN") or "", _DEFAULT_SIG_BN),
        show_logo=show_logo and logo is not None,
        show_signatures=show_sig,
    )


def format_doc_number(*, meeting_id: Optional[int], version: int, meeting_date_year: Optional[int] = None) -> str:
    """Document / minutes numbering line for the letterhead."""
    fmt = (os.getenv("MINUTES_DOC_NUMBER_FMT") or "MIN-{meeting_id}").strip() or "MIN-{meeting_id}"
    from datetime import datetime, timezone

    year = meeting_date_year or datetime.now(timezone.utc).year
    mid = meeting_id if meeting_id is not None else "—"
    try:
        return fmt.format(meeting_id=mid, version=version, year=year, doc_prefix=BRAND.doc_id_prefix)
    except (KeyError, ValueError, IndexError):
        return f"MIN-{mid}"
