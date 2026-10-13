"""Plain-language copy catalogue and display helpers for the Jinja templates.

The catalogue lives in ``copy_en.json``. Templates get
``t()``, ``status_label``/``status_tone``, ``reason_label``/``reason_fix`` and ``local_dt``.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

COPY_PATH = Path(__file__).resolve().parent / "copy_en.json"


@lru_cache(maxsize=1)
def catalogue() -> Dict[str, Any]:
    return json.loads(COPY_PATH.read_text(encoding="utf-8"))


def _lookup(key: str) -> Any:
    node: Any = catalogue()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def t(key: str, default: Optional[str] = None, **values: Any) -> str:
    """Catalogue string for a dotted key; falls back to ``default`` or the key itself."""
    node = _lookup(key)
    if isinstance(node, dict):
        node = node.get("label")
    text = node if isinstance(node, str) else (default if default is not None else key)
    return text.format(**values) if values else text


def _humanize(value: str) -> str:
    text = str(value or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _status_entry(kind: str, raw: Any) -> Any:
    group = _lookup(f"status.{kind}")
    return group.get(str(raw)) if isinstance(group, dict) else None


def status_label(value: Any, kind: str = "ingest") -> str:
    raw = getattr(value, "value", value)
    if raw is None or raw == "":
        return ""
    node = _status_entry(kind, raw)
    if isinstance(node, dict) and node.get("label"):
        return node["label"]
    return _humanize(str(raw))


def status_tone(value: Any, kind: str = "ingest") -> str:
    raw = getattr(value, "value", value)
    node = _status_entry(kind, raw)
    if isinstance(node, dict) and node.get("tone"):
        return node["tone"]
    return "neutral"


def reason_label(code: str) -> str:
    node = _lookup(f"reason.{code}")
    if isinstance(node, dict) and node.get("label"):
        return node["label"]
    return _humanize(code)


def reason_fix(code: str) -> str:
    node = _lookup(f"reason.{code}")
    return node.get("fix", "") if isinstance(node, dict) else ""


def reason_labels(codes: Optional[Iterable[str]]) -> List[str]:
    return [reason_label(c) for c in (codes or [])]


def role_label(role: Any) -> str:
    raw = getattr(role, "value", role)
    return t(f"role.{raw}", default=_humanize(str(raw or "")))


@lru_cache(maxsize=1)
def display_timezone() -> tzinfo:
    """Bank staff read times in Dhaka time. Bangladesh has no DST, so a fixed offset is a safe fallback."""
    name = os.getenv("UI_TIMEZONE", "Asia/Dhaka")
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=6), "BDT")


def _as_datetime(value: Union[datetime, date, str, None]) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def local_dt(value: Union[datetime, str, None], fmt: str = "%d %b %Y, %H:%M") -> str:
    """A system timestamp (stored in UTC) shown in Dhaka time."""
    dt = _as_datetime(value)
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(display_timezone()).strftime(fmt)


def local_date(value: Union[datetime, date, str, None]) -> str:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.strftime("%d %b %Y")
    return local_dt(value, "%d %b %Y")


def wall_dt(value: Union[datetime, str, None], fmt: str = "%d %b %Y, %H:%M") -> str:
    """A wall-clock time entered by a person (for example a meeting time); shown as entered."""
    dt = _as_datetime(value)
    return dt.strftime(fmt) if dt else ""


def file_size(num: Optional[int]) -> str:
    if not num:
        return ""
    size = float(num)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


_ERROR_PATTERNS = (
    (re.compile(r"already exists", re.I), "error.duplicate_id"),
    (re.compile(r"disposed record", re.I), "error.doc_id_reserved"),
    (re.compile(r"(413|too large|exceeds)", re.I), "error.too_large"),
    (re.compile(r"(could not extract|no text|pdf.*(invalid|corrupt|damaged)|EOF marker|PdfReadError|not a pdf)", re.I), "error.unreadable_pdf"),
    (re.compile(r"(unsupported|content type|file type)", re.I), "error.unsupported_file"),
    (re.compile(r"(timed? ?out|connection|unreachable|temporarily)", re.I), "error.network"),
)


def friendly_error(raw: Optional[str]) -> str:
    """Plain message for a processing failure; the raw text stays in logs and admin views."""
    text = str(raw or "")
    for pattern, key in _ERROR_PATTERNS:
        if pattern.search(text):
            return t(key)
    return t("error.processing_failed")


def copy_section(name: str) -> Dict[str, Any]:
    """One top-level catalogue section, for pages whose scripts need the same wording."""
    node = catalogue().get(name)
    return node if isinstance(node, dict) else {}


def register(env: Any) -> None:
    from .nav import shell as nav_shell

    env.globals.update(
        nav_shell=nav_shell,
        copy_section=copy_section,
        t=t,
        status_label=status_label,
        status_tone=status_tone,
        reason_label=reason_label,
        reason_fix=reason_fix,
        role_label=role_label,
        friendly_error=friendly_error,
    )
    env.filters.update(
        status_label=status_label,
        status_tone=status_tone,
        reason_label=reason_label,
        local_dt=local_dt,
        local_date=local_date,
        wall_dt=wall_dt,
        file_size=file_size,
    )
