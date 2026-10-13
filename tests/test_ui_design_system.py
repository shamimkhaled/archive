"""Design tokens meet WCAG AA in every theme and mode; every status and review reason has plain copy."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from bcp_project import ui
from bcp_project.models import AccessRequestStatus, MeetingStatus
from bcp_project.services import briefing, ingest, minutes
from bcp_project.services.metadata_suggest import REASON_CODES

TOKENS = Path(__file__).resolve().parents[1] / "src" / "bcp_project" / "static" / "css" / "tokens.css"
THEMES = ("green", "red", "yellow", "white")


def _blocks():
    css = re.sub(r"/\*.*?\*/", "", TOKENS.read_text(), flags=re.S)
    return [
        (sel.strip(), dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", body)))
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    ]


def _palette(theme: str, mode: str):
    blocks = _blocks()

    def merged(pred):
        out = {}
        for sel, values in blocks:
            if pred(sel):
                out.update(values)
        return out

    colours = merged(lambda s: s == ":root")
    colours.update(merged(lambda s: s == f'[data-theme="{theme}"]' or (theme == "green" and s.startswith(":root,"))))
    if mode == "dark":
        colours.update(merged(lambda s: s == '[data-mode="dark"]'))
        colours.update(merged(lambda s: f'[data-mode="dark"][data-theme="{theme}"]' in s))
    return colours


def _contrast(a: str, b: str) -> float:
    def lum(hex_colour: str) -> float:
        h = hex_colour.lstrip("#")
        channels = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


PAIRS = (
    ("text", "surface"),
    ("text", "bg"),
    ("text-secondary", "surface"),
    ("muted", "surface"),
    ("muted", "bg"),
    ("bcg", "surface"),
    ("on-primary", "bcg"),
    ("ok", "ok-soft"),
    ("warn", "warn-soft"),
    ("danger", "danger-soft"),
    ("info", "info-soft"),
    ("text", "bcg-soft"),
)


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("mode", ("light", "dark"))
def test_tokens_meet_wcag_aa(theme, mode):
    colours = _palette(theme, mode)
    failures = [
        (fg, bg, round(_contrast(colours[f"--{fg}"], colours[f"--{bg}"]), 2))
        for fg, bg in PAIRS
        if _contrast(colours[f"--{fg}"], colours[f"--{bg}"]) < 4.5
    ]
    assert not failures, f"{theme}/{mode}: {failures}"


def test_every_status_has_plain_copy():
    groups = {
        "ingest": [v for k, v in vars(ingest).items() if k.startswith("INGEST_") and isinstance(v, str)],
        "version": [v for k, v in vars(ingest).items() if k.startswith("VERSION_") and isinstance(v, str)],
        "meeting": [s.value for s in MeetingStatus],
        "access": [s.value for s in AccessRequestStatus] + ["expired"],
        "minutes": [minutes.MINUTES_DRAFT, minutes.MINUTES_IN_REVIEW, minutes.MINUTES_APPROVED],
        "action": list(minutes.ACTION_STATUSES),
        "briefing_state": [briefing.STATE_AI_DRAFT, briefing.STATE_REVIEWED, briefing.STATE_APPROVED],
        "briefing_status": [briefing.STATUS_PENDING, briefing.STATUS_GENERATING, briefing.STATUS_READY, briefing.STATUS_FAILED],
    }
    catalogue = ui.catalogue()["status"]
    missing = [f"{kind}.{value}" for kind, values in groups.items() for value in values if value not in catalogue[kind]]
    assert not missing


def test_every_review_reason_has_label_and_fix():
    for code in REASON_CODES:
        assert ui.reason_label(code) != code
        assert ui.reason_fix(code)
    assert "OCR" not in " ".join(ui.reason_label(c) for c in REASON_CODES)


def test_local_dt_shows_dhaka_time():
    assert ui.local_dt(datetime(2026, 10, 13, 9, 20)) == "13 Oct 2026, 15:20"
    assert ui.local_dt("2026-10-13T09:20:00") == "13 Oct 2026, 15:20"
    assert ui.wall_dt(datetime(2026, 10, 13, 15, 0)) == "13 Oct 2026, 15:00"


def test_friendly_error_hides_exception_text():
    message = ui.friendly_error("ValueError: Could not extract text from PDF")
    assert "ValueError" not in message
    assert message == ui.t("error.unreadable_pdf")
    assert ui.friendly_error("RuntimeError: boom") == ui.t("error.processing_failed")
    reserved = ui.friendly_error("This Document ID belonged to a disposed record and cannot be reused")
    assert reserved == ui.t("error.doc_id_reserved")
