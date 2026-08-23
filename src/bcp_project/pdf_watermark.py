"""Server-side PDF watermark stamping for the secure viewer."""

from __future__ import annotations

import io
from typing import Dict, Tuple

from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject

try:
    from reportlab.lib.colors import Color
    from reportlab.pdfgen import canvas as rl_canvas
except ImportError:  # pragma: no cover
    Color = None  # type: ignore
    rl_canvas = None  # type: ignore


def _escape_pdf_literal(text: str) -> str:
    # Helvetica watermark page is Latin-1; drop unsupported glyphs for the seal.
    encoded = text.encode("latin-1", errors="replace").decode("latin-1")
    return encoded.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _make_watermark_page_pdf_ops(width: float, height: float, text: str) -> bytes:
    """Minimal one-page PDF watermark without third-party drawing libs."""
    safe = _escape_pdf_literal(text[:120])
    # Draw a few diagonal lines of text via content stream.
    content_parts = ["BT", "/F1 12 Tf", "0.05 0.2 0.15 rg"]
    y = 40.0
    row = 0
    while y < height:
        x = 20.0 + (15.0 if row % 2 else 0.0)
        while x < width:
            content_parts.append("q")
            content_parts.append(f"1 0 0 1 {x:.2f} {y:.2f} cm")
            content_parts.append("0.866 -0.5 0.5 0.866 0 0 cm")  # ~30°
            content_parts.append(f"({safe}) Tj")
            content_parts.append("Q")
            x += 260.0
        y += 140.0
        row += 1
    content_parts.append("ET")
    stream = "\n".join(content_parts).encode("latin-1", errors="replace")

    objects = []
    objects.append(b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n")
    objects.append(b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n")
    objects.append(
        (
            f"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width:.2f} {height:.2f}] "
            f"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>endobj\n"
        ).encode("latin-1")
    )
    objects.append(
        f"4 0 obj<< /Length {len(stream)} >>stream\n".encode("latin-1") + stream + b"\nendstream\nendobj\n"
    )
    objects.append(b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(out.tell())
        out.write(obj)
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(offsets)}\n".encode("latin-1"))
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode("latin-1"))
    out.write(
        f"trailer<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("latin-1")
    )
    return out.getvalue()


def _make_watermark_page_reportlab(width: float, height: float, text: str) -> bytes:
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(width, height))
    c.setFillColor(Color(0.05, 0.2, 0.15, alpha=0.22))
    c.setFont("Helvetica", 14)
    step_x, step_y = 280, 180
    y = -50
    while y < height + 100:
        x = -50
        while x < width + 100:
            c.saveState()
            c.translate(x, y)
            c.rotate(30)
            c.drawCentredString(0, 0, text[:120])
            c.restoreState()
            x += step_x
        y += step_y
    c.save()
    return buf.getvalue()


def _make_watermark_page(width: float, height: float, text: str) -> bytes:
    if rl_canvas is not None and Color is not None:
        try:
            return _make_watermark_page_reportlab(width, height, text)
        except Exception:
            pass
    return _make_watermark_page_pdf_ops(width, height, text)


def stamp_pdf_bytes(pdf_bytes: bytes, seal_text: str) -> bytes:
    """
    Return a new PDF with a diagonal identity watermark burned onto every page.
    Raises on unrecoverable PDF parse/merge errors (caller should map to 502).
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    watermark_cache: Dict[Tuple[float, float], PdfReader] = {}

    for page in reader.pages:
        box: RectangleObject = page.mediabox
        width = float(box.width)
        height = float(box.height)
        key = (round(width, 1), round(height, 1))
        if key not in watermark_cache:
            wm_bytes = _make_watermark_page(width, height, seal_text)
            watermark_cache[key] = PdfReader(io.BytesIO(wm_bytes))
        wm_page = watermark_cache[key].pages[0]
        page.merge_page(wm_page)
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
