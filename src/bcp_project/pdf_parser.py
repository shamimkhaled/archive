from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

try:
    from llama_parse import PDFParser
except ImportError:
    PDFParser = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from pdf2image import convert_from_path
except ImportError:
    convert_from_path = None

try:
    import pytesseract
except ImportError:
    pytesseract = None

@dataclass
class Document:
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)


def _ocr_languages() -> str:
    """Prefer Eng+Bangla when Tessdata is installed; fall back to English-only."""
    if pytesseract is None:
        return "eng"
    try:
        available = set(pytesseract.get_languages(config=""))
    except Exception:
        return "eng"
    langs = []
    if "eng" in available:
        langs.append("eng")
    if "ben" in available:
        langs.append("ben")
    return "+".join(langs) if langs else "eng"


def _ocr_pdf(source: Path) -> List[Document]:
    if convert_from_path is None or pytesseract is None:
        raise RuntimeError(
            "PDF appears to contain image-based pages. Install `pdf2image` and `pytesseract` "
            "to enable OCR fallback, and install Poppler on your system for pdf2image."
        )

    try:
        images = convert_from_path(str(source), dpi=300)
    except Exception as exc:
        raise RuntimeError(
            "Failed to convert PDF pages into images for OCR. Ensure Poppler is installed "
            "and available on your PATH, and that the PDF file is valid."
        ) from exc

    ocr_lang = _ocr_languages()
    documents: List[Document] = []
    for page_number, image in enumerate(images, start=1):
        text = pytesseract.image_to_string(image, lang=ocr_lang)
        cleaned = text.strip()
        if not cleaned:
            continue
        documents.append(
            Document(
                text=cleaned,
                metadata={"source": str(source), "page_number": page_number, "ocr_lang": ocr_lang},
            )
        )
    return documents


def parse_pdf(file_path: str) -> List[Document]:
    """Parse a PDF into page-level documents with page metadata."""
    source = Path(file_path)
    if not source.exists():
        raise FileNotFoundError(f"PDF file not found: {source}")

    if PDFParser is not None:
        parser = PDFParser()
        parsed = parser.parse(str(source))
        pages = getattr(parsed, "pages", None)
        if pages is not None:
            documents: List[Document] = []
            for page_number, page in enumerate(pages, start=1):
                text = getattr(page, "text", None) or str(page)
                cleaned = text.strip()
                if not cleaned:
                    continue
                documents.append(
                    Document(
                        text=cleaned,
                        metadata={"source": str(source), "page_number": page_number},
                    )
                )
            if documents:
                return documents

    if PdfReader is None:
        raise RuntimeError("Install llama-parse or pypdf to parse PDF documents.")

    reader = PdfReader(str(source))
    documents: List[Document] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        cleaned = text.strip()
        if not cleaned:
            continue
        documents.append(
            Document(
                text=cleaned,
                metadata={"source": str(source), "page_number": page_number},
            )
        )

    if documents:
        return documents

    return _ocr_pdf(source)
