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

``--sets stt`` builds two-speaker meeting dialogues with espeak-ng instead: a "Chair" and a "CFO"
voice alternate turns read from the corpus, in Bangla, English, or Bangla/English code-switching,
with the reference speaker turns timed exactly. Synthetic speech is far cleaner and more regular
than a boardroom, so this measures the speaker-attribution plumbing, not real accuracy; real
30-60 minute recordings are needed for that.

  PYTHONPATH=src python scripts/make_synthetic_benchmarks.py --out eval/benchmarks/synthetic
  PYTHONPATH=src python scripts/make_synthetic_benchmarks.py --sets stt
"""

from __future__ import annotations

import argparse
import io
import json
import random
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any, Dict, List

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


# --- Two-speaker meeting audio ---------------------------------------------------------------

# Distinct espeak-ng voice variants and pitch so the two speakers are acoustically separable.
SPEAKERS = {
    "Chair": {"variant": "m3", "pitch": 35, "speed": 150},
    "CFO": {"variant": "f2", "pitch": 70, "speed": 165},
}
VOICES = {"bn": "bn", "en": "en-gb"}
STT_PLANS = (("bn", "bn"), ("en", "en"), ("bn", "en"), ("en", "bn"))
TURN_GAP_S = 0.6


def sentences(text: str) -> List[str]:
    parts = re.split(r"(?<=[।.!?])\s+", text.replace("\n", " "))
    return [p.strip() for p in parts if 4 <= len(p.split()) <= 30]


def speak(text: str, lang: str, speaker: str, work: Path, n: int) -> tuple:
    """(frames, params) of one turn spoken by espeak-ng."""
    voice = SPEAKERS[speaker]
    out = work / f"turn-{n}.wav"
    subprocess.run(
        ["espeak-ng", "-v", f"{VOICES[lang]}+{voice['variant']}", "-p", str(voice["pitch"]), "-s", str(voice["speed"]), "-w", str(out), text],
        check=True,
        capture_output=True,
    )
    with wave.open(str(out), "rb") as wav:
        return wav.readframes(wav.getnframes()), wav.getparams()


def build_stt(docs: List[Dict[str, Any]], out: Path, rng: random.Random, samples: int, turns_per_sample: int) -> List[Dict[str, Any]]:
    if not shutil.which("espeak-ng"):
        raise SystemExit("espeak-ng is not installed; it is needed to synthesise the STT set.")
    pool = {"bn": [], "en": []}
    for doc in docs:
        for page in doc["pages"]:
            for sentence in sentences(page):
                pool[page_language(sentence)].append(sentence)
    for lang, items in pool.items():
        if len(items) < turns_per_sample:
            raise SystemExit(f"Corpus has too few {lang} sentences for the STT set")
    audio_dir = out / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for index in range(samples):
            chair_lang, cfo_lang = STT_PLANS[index % len(STT_PLANS)]
            frames: List[bytes] = []
            params = None
            cursor = 0.0
            turns = []
            plan = [("Chair", chair_lang) if n % 2 == 0 else ("CFO", cfo_lang) for n in range(turns_per_sample)]
            draws = {lang: rng.sample(pool[lang], sum(1 for _, l in plan if l == lang)) for lang in {l for _, l in plan}}
            for n, (speaker, lang) in enumerate(plan):
                text = draws[lang].pop()
                data, params = speak(text, lang, speaker, work, n)
                seconds = len(data) / (params.sampwidth * params.nchannels) / params.framerate
                turns.append({"speaker": speaker, "start": round(cursor, 2), "end": round(cursor + seconds, 2), "text": text, "lang": lang})
                gap = b"\x00" * (int(TURN_GAP_S * params.framerate) * params.sampwidth * params.nchannels)
                frames += [data, gap]
                cursor += seconds + TURN_GAP_S
            file = audio_dir / f"meeting-{index + 1:02d}.wav"
            with wave.open(str(file), "wb") as wav:
                wav.setparams(params)
                wav.writeframes(b"".join(frames))
            lang = chair_lang if chair_lang == cfo_lang else "mixed"
            rows.append(
                {
                    "id": file.stem,
                    "audio": f"audio/{file.name}",
                    "lang": lang,
                    "condition": "synthetic-tts",
                    "speakers": 2,
                    "reference": " ".join(t["text"] for t in turns),
                    "turns": turns,
                }
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", type=Path, default=ROOT / "eval" / "corpus_bilingual.json")
    parser.add_argument("--queries", type=Path, default=ROOT / "eval" / "retrieval_bilingual.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "eval" / "benchmarks" / "synthetic")
    parser.add_argument("--qualities", default="clean,scan,poor")
    parser.add_argument("--limit", type=int, help="Only the first N documents")
    parser.add_argument("--seed", type=int, default=20261012)
    parser.add_argument("--sets", default="pages,stt", help="pages (ocr, metadata, ask) and/or stt")
    parser.add_argument("--stt-samples", type=int, default=8)
    parser.add_argument("--stt-turns", type=int, default=8)
    args = parser.parse_args()
    sets = {s.strip() for s in args.sets.split(",") if s.strip()}

    rng = random.Random(args.seed)
    docs = json.loads(args.corpus.read_text(encoding="utf-8"))[: args.limit]
    if "stt" in sets:
        stt_rows = build_stt(docs, args.out, random.Random(args.seed), args.stt_samples, args.stt_turns)
        path = args.out / "stt.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in stt_rows), encoding="utf-8")
        print(f"wrote {path} ({len(stt_rows)} samples)")
    if "pages" not in sets:
        return 0

    if not features.check("raqm"):
        raise SystemExit("Pillow was built without libraqm; Bangla would render without shaping. Install libraqm.")
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
