#!/usr/bin/env python3
"""Build a synthetic bilingual benchmark set from eval/corpus_bilingual.json.

Each corpus page is typeset (with complex-script shaping, so Bangla conjuncts and vowel signs are
real glyph clusters), then degraded into three scan qualities and saved as an image-only PDF:

  clean  300 dpi, no noise                       — a good flatbed scan
  scan   200 dpi, 1.2° skew, blur, sensor noise  — a typical branch scanner
  poor   150 dpi, 2.5° skew, heavy noise, JPEG   — a photocopy of a fax

It also writes the matching manifests for scripts/bench_accuracy.py (ocr, metadata, ask). The
set measures the pipeline and catches regressions; it does not replace the bank's own sample of
real board papers, which the POC must run through the same commands.

  PYTHONPATH=src python scripts/make_synthetic_benchmarks.py --out eval/benchmarks/synthetic
"""

from __future__ import annotations

import argparse
import io
import json
import random
import subprocess
from pathlib import Path
from typing import Dict, List

from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

ROOT = Path(__file__).resolve().parents[1]
A4_INCHES = (8.27, 11.69)
QUALITIES: Dict[str, Dict[str, float]] = {
    "clean": {"dpi": 300, "skew": 0.0, "blur": 0.0, "noise": 0.0, "jpeg": 0},
    "scan": {"dpi": 200, "skew": 1.2, "blur": 0.6, "noise": 10.0, "jpeg": 0},
    "poor": {"dpi": 150, "skew": 2.5, "blur": 0.9, "noise": 22.0, "jpeg": 30},
}


def font_file(family: str) -> str:
    out = subprocess.run(["fc-match", "-f", "%{file}", family], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def load_font(lang: str, px: int) -> ImageFont.FreeTypeFont:
    family = "Noto Sans Bengali" if lang == "bn" else "Noto Serif"
    return ImageFont.truetype(font_file(family), px, layout_engine=ImageFont.Layout.RAQM)


def wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> List[str]:
    lines: List[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if current and font.getlength(trial) > width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def typeset(text: str, lang: str, dpi: int) -> Image.Image:
    width, height = int(A4_INCHES[0] * dpi), int(A4_INCHES[1] * dpi)
    margin = int(dpi * 0.9)
    font = load_font(lang, int(dpi * 12 / 72))
    page = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(page)
    y = margin
    line_height = int(font.size * 1.7)
    for paragraph in text.split("\n"):
        for line in wrap(paragraph, font, width - 2 * margin):
            draw.text((margin, y), line, font=font, fill=0, language="bn" if lang == "bn" else "en")
            y += line_height
        y += line_height // 2
    return page


def degrade(page: Image.Image, quality: Dict[str, float], rng: random.Random) -> Image.Image:
    if quality["skew"]:
        angle = quality["skew"] * rng.choice((-1, 1))
        page = page.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
    if quality["blur"]:
        page = page.filter(ImageFilter.GaussianBlur(quality["blur"]))
    if quality["noise"]:
        noise = Image.effect_noise(page.size, quality["noise"])
        page = Image.blend(page, noise, 0.18)
    if quality["jpeg"]:
        buf = io.BytesIO()
        page.save(buf, "JPEG", quality=int(quality["jpeg"]))
        page = Image.open(io.BytesIO(buf.getvalue())).convert("L")
    return page


def page_language(text: str) -> str:
    bangla = sum(1 for ch in text if "\u0980" <= ch <= "\u09ff")
    letters = sum(1 for ch in text if ch.isalpha())
    return "bn" if letters and bangla / letters > 0.3 else "en"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, default=ROOT / "eval" / "corpus_bilingual.json")
    parser.add_argument("--queries", type=Path, default=ROOT / "eval" / "retrieval_bilingual.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "eval" / "benchmarks" / "synthetic")
    parser.add_argument("--qualities", default="clean,scan,poor")
    parser.add_argument("--limit", type=int, help="Only the first N documents")
    parser.add_argument("--seed", type=int, default=20261012)
    args = parser.parse_args()

    if not features.check("raqm"):
        raise SystemExit("Pillow was built without libraqm; Bangla would render without shaping. Install libraqm.")

    rng = random.Random(args.seed)
    docs = json.loads(args.corpus.read_text(encoding="utf-8"))[: args.limit]
    pages_dir = args.out / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    ocr_rows, metadata_rows = [], []
    for doc in docs:
        for index, text in enumerate(doc["pages"], 1):
            lang = page_language(text)
            for name in args.qualities.split(","):
                quality = QUALITIES[name]
                image = degrade(typeset(text, lang, int(quality["dpi"])), quality, rng)
                file = pages_dir / f"{doc['doc_id']}-p{index}-{name}.pdf"
                image.save(file, "PDF", resolution=quality["dpi"])
                ocr_rows.append(
                    {"id": file.stem, "file": f"pages/{file.name}", "page": 1, "lang": lang, "quality": name, "reference": text}
                )
        metadata_rows.append(
            {
                "id": doc["doc_id"],
                "text": "\n\n".join(doc["pages"]),
                "lang": page_language(doc["pages"][0]),
                "expected": {
                    "doc_type": doc.get("doc_type"),
                    "title": doc.get("title"),
                    "department": doc.get("department"),
                    "category": doc.get("category"),
                    "doc_date": doc.get("doc_date"),
                },
            }
        )

    ask_rows = []
    known = {d["doc_id"] for d in docs}
    if args.queries.exists():
        for line in args.queries.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            q = json.loads(line)
            relevant = [d for d in q.get("relevant", []) if d in known]
            if not relevant:
                continue
            ask_rows.append(
                {
                    "id": q["id"],
                    "question": q["q"],
                    "lang": q.get("lang"),
                    "expect_docs": relevant,
                    "expect_answer": [q["answer"]] if q.get("answer") else [],
                    "should_refuse": False,
                }
            )

    for name, rows in (("ocr", ocr_rows), ("metadata", metadata_rows), ("ask", ask_rows)):
        path = args.out / f"{name}.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        print(f"wrote {path} ({len(rows)} samples)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
