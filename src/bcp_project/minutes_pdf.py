"""Render approved minutes (Markdown subset) to a PDF and report the text on each page.

The page texts are what gets indexed, so search hits and Ask Sonali citations point
at the same page numbers the viewer shows. Bangla runs use a Bengali TTF when one is
installed (``MINUTES_BANGLA_FONT`` or a system Noto/Lohit/FreeSerif font). With
``uharfbuzz`` and a TrueType Latin font (``MINUTES_LATIN_FONT`` or DejaVu/Noto Sans)
installed, ReportLab shapes conjuncts and vowel signs correctly.
"""

from __future__ import annotations

import io
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

logger = logging.getLogger("bcp_project.minutes_pdf")

BANGLA_FONT = "SBBangla"
# Bengali block plus the dandas (U+0964/65, shared with Devanagari) and ZWNJ/ZWJ.
_BN = "\u0980-\u09FF\u0964\u0965\u200c\u200d"
_BANGLA_RUN = re.compile(rf"([{_BN}]+(?:[\s,.;:()\-–—]+[{_BN}]+)*)")
_MARK = re.compile(r"\*\*(.+?)\*\*|(?<!\w)_(.+?)_(?!\w)")
LATIN_FONT = "SBLatin"
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
    "/usr/share/fonts/noto/NotoSansBengali-Regular.ttf",
    "/usr/share/fonts/truetype/lohit-bengali/Lohit-Bengali.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
)
# (regular, bold). Shaped paragraphs need a TrueType Latin face: ReportLab cannot shape
# a word whose clusters fall in a Type1 font such as Helvetica ("ঋণ(loan)").
_LATIN_CANDIDATES = (
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    ("/usr/share/fonts/noto/NotoSans-Regular.ttf", "/usr/share/fonts/noto/NotoSans-Bold.ttf"),
    ("/usr/share/fonts/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/liberation/LiberationSans-Bold.ttf"),
)
_registered: Dict[str, bool] = {}


def _register_family(name: str, regular: str, bold: Optional[str] = None) -> None:
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    pdfmetrics.registerFont(TTFont(name, regular))
    bold_name = name
    if bold and Path(bold).is_file():
        bold_name = f"{name}-Bold"
        pdfmetrics.registerFont(TTFont(bold_name, bold))
    for italic in (0, 1):
        addMapping(name, 0, italic, name)
        addMapping(name, 1, italic, bold_name)


def _bangla_font() -> bool:
    """Register the Bengali font once. False when none is installed (Bangla then renders as boxes)."""
    if "bangla" not in _registered:
        _registered["bangla"] = False
        configured = (os.getenv("MINUTES_BANGLA_FONT") or "").strip()
        for path in ([configured] if configured else []) + list(_FONT_CANDIDATES):
            if path and Path(path).is_file():
                try:
                    _register_family(BANGLA_FONT, path)
                    _registered["bangla"] = True
                    break
                except Exception as exc:
                    logger.warning("Could not register Bangla font %s: %s", path, exc)
        if not _registered["bangla"]:
            logger.warning("No Bengali font found; set MINUTES_BANGLA_FONT for Bangla minutes PDFs")
    return _registered["bangla"]


def _latin_ttf() -> bool:
    if "latin" not in _registered:
        _registered["latin"] = False
        configured = (os.getenv("MINUTES_LATIN_FONT") or "").strip()
        candidates = ([(configured, os.getenv("MINUTES_LATIN_BOLD_FONT") or None)] if configured else []) + list(
            _LATIN_CANDIDATES
        )
        for regular, bold in candidates:
            if Path(regular).is_file():
                try:
                    _register_family(LATIN_FONT, regular, bold)
                    _registered["latin"] = True
                    break
                except Exception as exc:
                    logger.warning("Could not register Latin font %s: %s", regular, exc)
    return _registered["latin"]


def _can_shape() -> bool:
    from reportlab.pdfbase import ttfonts

    return getattr(ttfonts, "uharfbuzz", None) is not None and _bangla_font() and _latin_ttf()


def _segments(text: str) -> List[Tuple[str, Optional[str]]]:
    """Split on **bold** / _italic_ markers into flat (text, tag) runs."""
    out: List[Tuple[str, Optional[str]]] = []
    pos = 0
    for m in _MARK.finditer(text):
        if m.start() > pos:
            out.append((text[pos : m.start()], None))
        out.append((m.group(1), "b") if m.group(1) is not None else (m.group(2), "i"))
        pos = m.end()
    out.append((text[pos:], None))
    return [s for s in out if s[0]]


def _inline(text: str, *, bold: bool = False, shape: bool = False) -> Tuple[str, bool]:
    """Paragraph markup with an explicit face per script run, and whether it needs shaping.

    Tags stay flat (one ``<font>`` per run) so bold spanning both scripts never mis-nests.
    """
    bangla = _bangla_font()
    shaped = shape and bangla and bool(_BANGLA_RUN.search(text))
    if shaped:
        latin_face = LATIN_FONT
    else:
        latin_face = "Helvetica-Bold" if bold else "Helvetica"
    parts: List[str] = []
    previous: Tuple[str, str] = ("", "")  # (face, last character)
    for segment, tag in _segments(text):
        runs = _BANGLA_RUN.split(segment) if bangla else [segment]
        for k, run in enumerate(runs):
            if not run:
                continue
            face = BANGLA_FONT if k % 2 else latin_face
            markup = escape(run)
            if bold and shaped:
                tag = "b"
            if tag:
                markup = f"<{tag}>{markup}</{tag}>"
            # The shaper draws a whole word in one font, so a script change inside a word
            # ("FY27-এর") would show missing-glyph boxes; split it with a space instead.
            if shaped and previous[0] and previous[0] != face and not previous[1].isspace() and not run[0].isspace():
                parts.append(" ")
            parts.append(f'<font face="{face}">{markup}</font>')
            previous = (face, run[-1])
    return "".join(parts), shaped


def _plain(text: str) -> str:
    return _MARK.sub(lambda m: m.group(1) or m.group(2), text).strip()


def _drawn_text(paragraph) -> str:
    """Text of a laid-out Paragraph (or a split half of one, which has no plain text of its own)."""
    bl = getattr(paragraph, "blPara", None)
    if bl is None:
        return paragraph.getPlainText()
    if bl.kind == 0:
        return " ".join(" ".join(line[1]) for line in bl.lines)
    return " ".join("".join(getattr(w, "text", "") for w in line.words) for line in bl.lines)


def _table_cells(line: str) -> List[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: List[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells if c) and any(cells)


def render_minutes_pdf(**kwargs) -> Tuple[bytes, List[str]]:
    """Return (pdf_bytes, page_texts) where ``page_texts[i]`` is the text on page i+1.

    Keyword arguments: title, body_md, meeting_date, location, version, approved_by,
    approved_at, sha256, and optionally label (footer wording, e.g. "Meeting transcript"). Shaped Bangla is tried first; if shaping fails on some input the
    PDF is rendered unshaped rather than blocking approval.
    """
    if _can_shape():
        try:
            return _render(shape=True, **kwargs)
        except Exception:
            logger.exception("Shaped minutes PDF failed; rendering without Bangla shaping")
    return _render(shape=False, **kwargs)


def _render(
    *,
    shape: bool,
    title: str,
    body_md: str,
    meeting_date: Optional[datetime],
    location: Optional[str],
    version: int,
    approved_by: str,
    approved_at: datetime,
    sha256: str,
    label: str = "Approved minutes",
) -> Tuple[bytes, List[str]]:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    body = ParagraphStyle("mbody", parent=styles["BodyText"], fontSize=10.5, leading=15)
    bullet = ParagraphStyle("mbullet", parent=body, leftIndent=12, bulletIndent=2)
    heads = {1: styles["Title"], 2: styles["Heading2"], 3: styles["Heading3"]}
    cell = ParagraphStyle("mcell", parent=body, fontSize=9.5, leading=12)

    page_texts: Dict[int, List[str]] = {}

    class _Doc(SimpleDocTemplate):
        def afterFlowable(self, flowable):
            # Paragraph halves split across pages are new objects; read their own text.
            source = getattr(flowable, "source_text", None)
            if source is None and isinstance(flowable, Paragraph):
                source = _drawn_text(flowable)
            if source:
                page_texts.setdefault(self.page, []).append(source)

    def tagged(flowable, source: str):
        flowable.source_text = source
        return flowable

    class _ShapedParagraph(Paragraph):
        """Shaped lines hold glyph clusters, not source text, so carry the source along.
        A split keeps it on the first half: page attribution may be one page early, never lost."""

        def split(self, availWidth, availHeight):
            parts = super().split(availWidth, availHeight)
            if len(parts) > 1:
                parts[0].source_text = getattr(self, "source_text", "")
                for part in parts[1:]:
                    part.source_text = ""
            return parts

    shaped: Dict[str, ParagraphStyle] = {}

    def para(text: str, style: ParagraphStyle, **kwargs) -> Paragraph:
        # ReportLab only shapes when the paragraph's base font is shapable, so Bangla
        # paragraphs switch base font to the Bengali TTF.
        markup, needs_shaping = _inline(text, bold=style.fontName == "Helvetica-Bold", shape=shape)
        if needs_shaping:
            if style.name not in shaped:
                shaped[style.name] = ParagraphStyle(
                    style.name + "_bn", parent=style, fontName=BANGLA_FONT, shaping=1
                )
            return tagged(_ShapedParagraph(markup, shaped[style.name], **kwargs), _plain(text))
        return Paragraph(markup, style, **kwargs)

    footer = (
        f"{label} v{version} · approved by {approved_by} on {approved_at.strftime('%Y-%m-%d %H:%M')} UTC · "
        f"SHA-256 {sha256[:16]}…"
    )

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.grey)
        canvas.drawString(18 * mm, 10 * mm, footer)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    when = meeting_date.strftime("%d %b %Y, %H:%M UTC") if meeting_date else "—"
    header = f"Meeting: {title} · {when} · {location or '—'}"
    story = [para(header, body), Spacer(1, 6)]

    lines = (body_md or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if not stripped or stripped == "---":
            i += 1
            continue
        if stripped.startswith("|"):
            rows: List[List[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = _table_cells(lines[i])
                if not _is_separator(cells):
                    rows.append(cells)
                i += 1
            # One Table per row so a page break never drops a row's text from the index.
            width = max(len(r) for r in rows)
            col = (A4[0] - 36 * mm) / width
            for n, r in enumerate(rows):
                cells = r + [""] * (width - len(r))
                row = Table([[para(c, cell) for c in cells]], colWidths=[col] * width, hAlign="LEFT")
                row.setStyle(
                    TableStyle(
                        [("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]
                        + ([("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke)] if n == 0 else [])
                    )
                )
                story.append(tagged(row, " | ".join(_plain(c) for c in cells)))
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            story.append(para(heading.group(2), heads[len(heading.group(1))]))
        elif re.match(r"^[-*]\s+", stripped):
            story.append(para(re.sub(r"^[-*]\s+", "", stripped), bullet, bulletText="•"))
        else:
            story.append(para(stripped, body))
        i += 1

    buffer = io.BytesIO()
    doc = _Doc(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"{'Minutes' if label == 'Approved minutes' else label} — {title}",
        author=approved_by,
    )
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    pages = [("\n".join(page_texts.get(n, []))).strip() for n in range(1, max(page_texts or {1: []}) + 1)]
    return buffer.getvalue(), pages
