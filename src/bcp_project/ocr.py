"""Page text extraction with OCR confidence.

PDF pages with a usable text layer are read directly. Pages without one, and
image uploads, are rendered at 300 DPI, deskewed, binarized, and passed to
Tesseract with ``image_to_data`` so every word carries a confidence score.
"""

from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("bcp_project.ocr")

TARGET_DPI = 300


@dataclass
class PageText:
    page_number: int
    text: str
    confidence: Optional[float] = None
    source: str = "text"
    metadata: Dict[str, Any] = field(default_factory=dict)


def review_thresholds() -> Tuple[float, float]:
    """(mean threshold, single-page threshold), both 0-100."""
    return (
        float(os.getenv("OCR_REVIEW_THRESHOLD", "75")),
        float(os.getenv("OCR_PAGE_REVIEW_THRESHOLD", "60")),
    )


def ocr_languages() -> str:
    from .pdf_parser import _ocr_languages

    return os.getenv("OCR_LANGUAGES") or _ocr_languages()


def _otsu_threshold(gray) -> int:
    import numpy as np

    hist = np.bincount(gray.ravel(), minlength=256).astype(float)
    total = gray.size
    sum_all = float(np.dot(np.arange(256), hist))
    sum_b = weight_b = 0.0
    best, threshold = -1.0, 127
    for level in range(256):
        weight_b += hist[level]
        if weight_b == 0:
            continue
        weight_f = total - weight_b
        if weight_f == 0:
            break
        sum_b += level * hist[level]
        mean_b = sum_b / weight_b
        mean_f = (sum_all - sum_b) / weight_f
        between = weight_b * weight_f * (mean_b - mean_f) ** 2
        if between > best:
            best, threshold = between, level
    return threshold


def _skew_angle(binary, max_angle: float = 5.0, step: float = 0.5) -> float:
    """Angle that makes text rows sharpest (projection-profile method), on a downscaled copy."""
    import numpy as np
    from PIL import Image

    img = Image.fromarray(binary)
    scale = min(1.0, 1000.0 / max(img.size))
    if scale < 1.0:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    ink = Image.fromarray(255 - np.asarray(img))
    best_angle, best_score = 0.0, -1.0
    angle = -max_angle
    while angle <= max_angle + 1e-9:
        rotated = np.asarray(ink.rotate(angle, expand=False, fillcolor=0), dtype=float)
        score = float(np.var(rotated.sum(axis=1)))
        if score > best_score:
            best_angle, best_score = angle, score
        angle += step
    return best_angle


def preprocess(image):
    """Grayscale, upscale to ~300 DPI, deskew, Otsu binarize. Returns a PIL image."""
    import numpy as np
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(image).convert("L")
    dpi = image.info.get("dpi", (TARGET_DPI, TARGET_DPI))[0] or TARGET_DPI
    if dpi < TARGET_DPI * 0.9:
        factor = min(TARGET_DPI / float(dpi), 4.0)
        img = img.resize((int(img.width * factor), int(img.height * factor)), Image.LANCZOS)
    gray = np.asarray(img, dtype=np.uint8)
    threshold = _otsu_threshold(gray)
    binary = np.where(gray > threshold, 255, 0).astype(np.uint8)
    if os.getenv("OCR_DESKEW", "1") == "1":
        angle = _skew_angle(binary)
        if abs(angle) >= 0.25:
            rotated = img.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC)
            gray = np.asarray(rotated, dtype=np.uint8)
            binary = np.where(gray > _otsu_threshold(gray), 255, 0).astype(np.uint8)
    return Image.fromarray(binary)


@dataclass
class OcrResult:
    text: str
    confidence: Optional[float]
    ink_coverage: Optional[float] = None
    recovered_lines: int = 0
    bangla_suspect_words: int = 0


_BANGLA_CHAR = re.compile("[\u0980-\u09ff]")
_LATIN_OR_ASCII_DIGIT = re.compile("[A-Za-z0-9]")


def bangla_suspect_words(words: List[Dict[str, Any]], text: str) -> int:
    """Words on a Bangla page that Tesseract replaced with Latin or ASCII-digit junk.

    On Bangla pages Tesseract's page confidence does not track errors (eval/reports/ocr-synthetic.json:
    pages above 5% CER score 89-91 while clean pages go down to 82). What does track them is a whole
    word read as Latin ("অ্যান্ড কোং" -> "WIS (@12", "১১.২" -> "5502") with low word confidence.
    Mixed-digit numbers ("১0৮০০") and stray brackets are single-character slips and are not counted.
    """
    if not _mostly_bangla(text):
        return 0
    cutoff = float(os.getenv("OCR_BANGLA_SUSPECT_WORD_CONF", "50"))
    count = 0
    for word in words:
        token = word["text"]
        if word["conf"] < cutoff and _LATIN_OR_ASCII_DIGIT.search(token) and not _BANGLA_CHAR.search(token):
            count += 1
    return count


def _words(data: Dict[str, List[Any]], dx: int = 0, dy: int = 0) -> List[Dict[str, Any]]:
    words = []
    for i, word in enumerate(data.get("text", [])):
        word = (word or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not word or conf < 0:
            continue
        words.append(
            {
                "text": word,
                "conf": conf,
                "line": (data["block_num"][i], data["par_num"][i], data["line_num"][i]),
                "box": (data["left"][i] + dx, data["top"][i] + dy, data["width"][i], data["height"][i]),
            }
        )
    return words


def _uncovered_bands(ink, words: List[Dict[str, Any]]) -> Tuple[List[Tuple[int, int, int, int]], int]:
    """Horizontal bands of ink that no recognised line accounts for, plus the line height used.

    Tesseract's layout step sometimes drops a whole text line while the words it did read keep
    high confidence; Bangla lines, whose headstroke joins a word into one long blob, are the
    usual victims. Each recognised line's box (grown vertically for matras and descenders)
    is masked out; rows that still carry a line's worth of ink are grouped into bands.
    """
    import numpy as np

    heights = [w["box"][3] for w in words]
    line_h = int(np.median(heights)) if heights else 40
    covered = np.zeros(ink.shape, dtype=bool)
    lines: Dict[Tuple[int, int, int], List[int]] = {}
    for w in words:
        left, top, width, height = w["box"]
        box = lines.setdefault(w["line"], [left, top, left + width, top + height])
        box[0], box[1] = min(box[0], left), min(box[1], top)
        box[2], box[3] = max(box[2], left + width), max(box[3], top + height)
    grow = max(4, int(line_h * 0.45))
    for left, top, right, bottom in lines.values():
        covered[max(0, top - grow) : bottom + grow, max(0, left - grow) : right + grow] = True
    loose = ink & ~covered
    row_ink = loose.sum(axis=1)
    min_row = max(6, int(ink.shape[1] * 0.002))
    rows = np.flatnonzero(row_ink >= min_row)
    bands: List[Tuple[int, int, int, int]] = []
    if rows.size == 0:
        return bands, line_h
    gap = max(3, int(line_h * 0.2))
    start = prev = int(rows[0])
    for y in [int(r) for r in rows[1:]] + [None]:
        if y is not None and y - prev <= gap:
            prev = y
            continue
        if prev - start + 1 >= max(8, int(line_h * 0.4)):
            cols = np.flatnonzero(loose[start : prev + 1].any(axis=0))
            bands.append((start, prev + 1, int(cols[0]), int(cols[-1]) + 1))
        if y is not None:
            start = prev = y
    return bands, line_h


def ocr_image_detail(image, lang: Optional[str] = None) -> OcrResult:
    """Tesseract with a second pass for text lines the layout analysis skipped.

    ``ink_coverage`` is the share of the page's ink that ended up inside a read line. When it
    falls below ``OCR_MIN_INK_COVERAGE`` the page confidence is capped at that share, so a page
    with missing text goes to human verification even if the words that were read look certain.
    """
    import numpy as np
    import pytesseract

    lang = lang or ocr_languages()
    page = preprocess(image)
    words = _words(pytesseract.image_to_data(page, lang=lang, output_type=pytesseract.Output.DICT))
    lines: List[Tuple[float, Tuple[Any, ...], List[str]]] = []
    by_line: Dict[Tuple[int, int, int], List[str]] = {}
    tops: Dict[Tuple[int, int, int], int] = {}
    for w in words:
        by_line.setdefault(w["line"], []).append(w["text"])
        tops[w["line"]] = min(tops.get(w["line"], w["box"][1]), w["box"][1])
    for order, key in enumerate(sorted(by_line)):
        lines.append((order, key, by_line[key]))

    coverage: Optional[float] = None
    recovered = 0
    ink = np.asarray(page, dtype=np.uint8) < 128
    total_ink = int(ink.sum())
    # Recovery is for text pages: skip blank pages, pages where nothing was read, and photos or
    # noise (text rarely covers more than a fifth of a page).
    if os.getenv("OCR_RECOVER_LINES", "1") == "1" and words and 0 < total_ink < ink.size * 0.3:
        bands, line_h = _uncovered_bands(ink, words)
        missed_ink = 0
        pad = max(6, int(line_h * 0.35))
        max_bands = int(os.getenv("OCR_RECOVER_MAX_BANDS", "8"))
        bands.sort(key=lambda b: -int(ink[b[0] : b[1], b[2] : b[3]].sum()))
        for index, (top, bottom, left, right) in enumerate(bands):
            band_ink = int(ink[top:bottom, left:right].sum())
            area = max(1, (bottom - top) * (right - left))
            if index >= max_bands or bottom - top > line_h * 8 or band_ink / area > 0.45:
                missed_ink += band_ink
                continue
            box = (max(0, left - pad), max(0, top - pad), min(page.width, right + pad), min(page.height, bottom + pad))
            band_words: List[Dict[str, Any]] = []
            # One band can hold several skipped lines; raw-line mode (13) reads a single line,
            # block mode (6) the rest.
            for psm in ((13, 6) if bottom - top <= line_h * 1.8 else (6, 13)):
                data = pytesseract.image_to_data(page.crop(box), lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
                band_words = _words(data, dx=box[0], dy=box[1])
                if band_words and sum(w["conf"] for w in band_words) / len(band_words) >= 30:
                    break
                band_words = []
            if not band_words:
                missed_ink += band_ink
                continue
            recovered += 1
            words.extend(band_words)
            # Place the recovered text after the last line that starts above the band.
            position = sum(1 for key in tops if tops[key] < top) - 0.5
            lines.append((position, ("recovered", top), [w["text"] for w in band_words]))
        coverage = round(1 - missed_ink / total_ink, 4)

    text = "\n".join(" ".join(ws) for _, _, ws in sorted(lines, key=lambda item: (item[0], str(item[1]))))
    if not words:
        return OcrResult(text, None, coverage, recovered)
    weights = [len(w["text"]) for w in words]
    mean = sum(w["conf"] * n for w, n in zip(words, weights)) / max(1, sum(weights))
    minimum = float(os.getenv("OCR_MIN_INK_COVERAGE", "0.9"))
    if coverage is not None and coverage < minimum:
        mean = min(mean, coverage * 100)
    return OcrResult(text, round(mean, 2), coverage, recovered, bangla_suspect_words(words, text))


def ocr_image(image, lang: Optional[str] = None) -> Tuple[str, Optional[float]]:
    """Text in reading order plus the mean word confidence (0-100), or None if no words."""
    result = ocr_image_detail(image, lang)
    return result.text, result.confidence


def _pdf_text_layer(data: bytes) -> List[str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    texts = []
    for page in reader.pages:
        try:
            texts.append((page.extract_text() or "").strip())
        except Exception:
            texts.append("")
    return texts


def _render_pdf_page(data: bytes, page_number: int, dpi: int = TARGET_DPI):
    from pdf2image import convert_from_bytes

    images = convert_from_bytes(data, dpi=dpi, first_page=page_number, last_page=page_number)
    if not images:
        raise RuntimeError(f"Could not render page {page_number}")
    image = images[0]
    image.info["dpi"] = (dpi, dpi)
    return image


def render_page_png(data: bytes, page_number: int, dpi: int = 110) -> bytes:
    """One PDF page as PNG, for the reviewer's side-by-side view."""
    buffer = io.BytesIO()
    _render_pdf_page(data, page_number, dpi=dpi).save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# Bijoy / SutonnyMJ-style fonts map Bangla glyphs onto ANSI code points, so the text layer reads
# "‡mvbvjx e¨vsK" for "সোনালী ব্যাংক". † and ‡ carry the e-kar, which nearly every Bangla
# sentence has; the others are phala and reph positions. °, §, ± and ® are left out because
# English technical and legal text uses them.
_LEGACY_E_KAR = frozenset("†‡")
_LEGACY_BANGLA_MARKS = _LEGACY_E_KAR | frozenset("¨©ªÖ¯")


def legacy_bangla_text(text: str) -> bool:
    """True when a text layer looks like ANSI-encoded (Bijoy) Bangla rather than Unicode."""
    if os.getenv("OCR_DETECT_LEGACY_BANGLA", "1") == "0":
        return False
    chars = [c for c in text if not c.isspace()]
    if not chars or any("\u0980" <= c <= "\u09ff" for c in chars):
        return False
    e_kar = sum(1 for c in chars if c in _LEGACY_E_KAR)
    marks = sum(1 for c in chars if c in _LEGACY_BANGLA_MARKS)
    return e_kar >= 2 and marks / len(chars) >= 0.01


def extract_pdf(data: bytes) -> List[PageText]:
    min_chars = int(os.getenv("OCR_MIN_TEXT_CHARS", "25"))
    texts = _pdf_text_layer(data)
    lang = ocr_languages()
    pages: List[PageText] = []
    for number, text in enumerate(texts, start=1):
        legacy = len(text) >= min_chars and legacy_bangla_text(text)
        if len(text) >= min_chars and not legacy:
            pages.append(PageText(number, text, None, "text"))
            continue
        try:
            result = ocr_image_detail(_render_pdf_page(data, number), lang)
        except Exception as exc:
            logger.warning("OCR failed on page %s: %s", number, exc)
            result = OcrResult("" if legacy else text, 0.0)
        meta = _ocr_meta(lang, result)
        if legacy:
            meta["text_layer"] = "legacy_bangla"
        pages.append(
            PageText(number, result.text or ("" if legacy else text), result.confidence if result.confidence is not None else 0.0, "ocr", meta)
        )
    return pages


def _ocr_meta(lang: str, result: OcrResult) -> Dict[str, Any]:
    meta: Dict[str, Any] = {"ocr_lang": lang}
    if result.ink_coverage is not None:
        meta["ink_coverage"] = result.ink_coverage
    if result.recovered_lines:
        meta["recovered_lines"] = result.recovered_lines
    if result.bangla_suspect_words:
        meta["bangla_suspect_words"] = result.bangla_suspect_words
    return meta


def extract_image(data: bytes) -> List[PageText]:
    from PIL import Image, ImageSequence

    lang = ocr_languages()
    pages: List[PageText] = []
    with Image.open(io.BytesIO(data)) as img:
        dpi = img.info.get("dpi")
        for number, frame in enumerate(ImageSequence.Iterator(img), start=1):
            page = frame.copy()
            if dpi:
                page.info["dpi"] = dpi
            result = ocr_image_detail(page, lang)
            pages.append(
                PageText(number, result.text, result.confidence if result.confidence is not None else 0.0, "ocr", _ocr_meta(lang, result))
            )
    return pages


def extract_pages(data: bytes, content_type: str) -> List[PageText]:
    from .intake import IMAGE_TYPES

    return extract_image(data) if content_type in IMAGE_TYPES else extract_pdf(data)


def summarize_confidence(pages: List[PageText]) -> Dict[str, Any]:
    """Character-weighted mean over OCR pages; text-layer pages count as fully confident."""
    ocr_pages = [p for p in pages if p.source == "ocr"]
    if not ocr_pages:
        return {"mean": None, "min": None, "ocr_pages": 0, "needs_review": False}
    weights = [max(1, len(p.text)) for p in ocr_pages]
    confs = [p.confidence or 0.0 for p in ocr_pages]
    mean = round(sum(c * w for c, w in zip(confs, weights)) / sum(weights), 2)
    low = min(confs)
    mean_threshold, page_threshold = review_thresholds()
    # Tesseract's Bangla confidence is poorly calibrated (eval/reports/ocr-synthetic.json: pages at
    # 85-92 have the same error rate), so Bangla pages can be held to their own bar.
    bangla_threshold = float(os.getenv("OCR_BANGLA_PAGE_REVIEW_THRESHOLD") or page_threshold)
    bangla_low = any(_mostly_bangla(p.text) and (p.confidence or 0.0) < bangla_threshold for p in ocr_pages)
    # Confidence alone misses misread Bangla words, so suspect words also route a page to review.
    suspect_limit = int(os.getenv("OCR_BANGLA_SUSPECT_WORDS", "1"))
    suspect_pages = [p.page_number for p in ocr_pages if suspect_limit > 0 and (p.metadata or {}).get("bangla_suspect_words", 0) >= suspect_limit]
    empty = all(not p.text.strip() for p in pages)
    return {
        "mean": mean,
        "min": round(low, 2),
        "ocr_pages": len(ocr_pages),
        "bangla_suspect_pages": suspect_pages,
        "needs_review": empty or mean < mean_threshold or low < page_threshold or bangla_low or bool(suspect_pages),
    }


def _mostly_bangla(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(1 for c in letters if "\u0980" <= c <= "\u09ff") / len(letters) >= 0.5
