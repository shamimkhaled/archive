#!/usr/bin/env python3
"""Software component and licence list (post-demonstration report, section 9 item 4).

Builds the inventory from what is actually installed, not from memory:

  Python    requirements*.txt resolved transitively through installed package metadata
            (extras included where requested, e.g. uvicorn[standard])
  Browser   third-party files vendored under src/bcp_project/static (reviewed list)
  Runtime   services and AI models the deployment pulls in, with their licences (reviewed list)

Anything copyleft or without a declared licence is flagged for legal review.

  PYTHONPATH=src python scripts/license_inventory.py --out docs/evidence/licenses.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from importlib import metadata
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ("requirements.txt", "requirements-worker.txt", "requirements-optional.txt")

# Services and models the deployment runs alongside the code. Licences reviewed by hand; re-check
# when a version changes.
RUNTIME: List[Tuple[str, str, str, str]] = [
    ("PostgreSQL", "16+ / Neon", "PostgreSQL License", "Database"),
    ("Qdrant", "1.x", "Apache-2.0", "Vector search"),
    ("Redis", "7.2 (or Valkey 8)", "BSD-3-Clause (Redis ≤7.2, Valkey) / RSALv2-SSPLv1 (Redis 7.4+)", "Queue (Streams), cache, rate limits"),
    ("Tesseract OCR", "5.x", "Apache-2.0", "OCR engine"),
    ("tessdata ben, eng", "4.x LSTM", "Apache-2.0", "OCR language models"),
    ("Poppler (pdftoppm)", "24.x", "GPL-2.0-or-later", "PDF page rendering (separate executable, invoked as a process)"),
    ("FFmpeg", "6.x/7.x", "LGPL-2.1-or-later (GPL if built with GPL components)", "Audio normalisation (separate executable)"),
    ("ClamAV", "1.x", "GPL-2.0", "Malware scan of uploads (separate daemon over a socket)"),
    ("MinIO", "RELEASE.2025+", "AGPL-3.0", "On-premise S3 object storage (unmodified, separate service over the S3 API; commercial licence available, or use the bank's existing S3-compatible store)"),
    ("Nginx", "1.27", "BSD-2-Clause", "TLS termination, AI gateway"),
    ("Prometheus / Grafana", "2.x / 11.x", "Apache-2.0 / AGPL-3.0", "Monitoring (Grafana unmodified, separate service)"),
    ("Noto Sans Bengali / Noto Serif", "2.x", "OFL-1.1", "Fonts for PDF renditions and seals"),
    ("vLLM", "0.8.x", "Apache-2.0", "On-premise LLM server"),
    ("Text Embeddings Inference", "1.7", "Apache-2.0", "On-premise embeddings and reranking server"),
    ("speaches (faster-whisper server)", "0.8.x", "MIT", "On-premise speech-to-text server"),
    ("Qwen2.5-14B-Instruct / 32B-Instruct", "2.5", "Apache-2.0", "On-premise chat model (3B and 72B use the Qwen licence — avoid or review)"),
    ("BAAI bge-m3", "1.0", "MIT", "Embedding model (multilingual, Bangla)"),
    ("BAAI bge-reranker-v2-m3", "2.0", "Apache-2.0", "Reranking model"),
    ("Whisper large-v3 (faster-whisper conversion)", "v3", "MIT", "Speech-to-text model"),
]

# Third-party files served to browsers from src/bcp_project/static.
BROWSER: List[Tuple[str, str, str, str]] = [
    ("pdf.js (pdfjs-dist, legacy build)", "6.4.299", "Apache-2.0", "PDF viewer (static/vendor/pdfjs)"),
    ("Inter (variable)", "4.x", "OFL-1.1", "Interface font (static/fonts)"),
    ("Noto Sans Bengali (variable)", "2.x", "OFL-1.1", "Bangla interface font (static/fonts)"),
]

NOTES = {
    "fastembed": "PyPI classifier says Other/Proprietary; the source repository (qdrant/fastembed) is Apache-2.0. The archive code never imports it, so it can be removed from requirements.txt.",
    "certifi": "Mozilla CA bundle, used unmodified as data.",
    "psycopg": "Used unmodified as a dynamically loaded library; LGPL obligations are met by not modifying it.",
    "psycopg-binary": "As psycopg.",
    "psycopg2-binary": "As psycopg; only scripts use psycopg2.",
    "tqdm": "MPL-2.0 file-level copyleft; used unmodified.",
    "autocommand": "Pulled in by setuptools tooling, not used at runtime.",
    "py_rust_stemmers": "No licence metadata; pulled in only by fastembed and goes away with it.",
}

STRONG = re.compile(r"\b(A?GPL|GNU General Public|Affero|SSPL|RSAL|EUPL|OSL)", re.I)
WEAK = re.compile(r"\b(LGPL|GNU Lesser|MPL|Mozilla Public|EPL|CDDL)", re.I)
PERMISSIVE = re.compile(r"\b(MIT|BSD|Apache|ISC|PSF|Python Software Foundation|Unlicense|Zlib|0BSD|CC0|HPND|PostgreSQL|BlueOak|Artistic|OFL)", re.I)


def classify(licence: str) -> str:
    if not licence or licence.strip().upper() in ("UNKNOWN", "NOASSERTION"):
        return "unknown"
    options = re.split(r"\s+OR\s+", licence.strip("() "), flags=re.I)
    if len(options) > 1 and any(classify(o) == "permissive" for o in options):
        return "permissive"
    if STRONG.search(licence):
        return "copyleft"
    if WEAK.search(licence):
        return "weak copyleft"
    if PERMISSIVE.search(licence):
        return "permissive"
    return "review"


# --- Python -------------------------------------------------------------------------------------

_REQ = re.compile(r"^\s*([A-Za-z0-9_.\-]+)(\[[^\]]*\])?")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(paths: Iterable[Path]) -> Dict[str, Set[str]]:
    wanted: Dict[str, Set[str]] = {}
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            match = _REQ.match(line)
            if match:
                extras = {e.strip() for e in (match.group(2) or "").strip("[]").split(",") if e.strip()}
                wanted.setdefault(canonical(match.group(1)), set()).update(extras)
    return wanted


def dist_licence(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip()
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    classifiers = [c for c in classifiers if c not in ("OSI Approved",)]
    if classifiers:
        return "; ".join(sorted(set(classifiers)))
    text = (meta.get("License") or "").strip()
    if text and len(text) < 80 and "\n" not in text:
        return text
    if text:
        first = text.splitlines()[0].strip()
        return first[:80] + (" (full text in package)" if len(text) > 80 else "")
    return "UNKNOWN"


def resolve_python(wanted: Dict[str, Set[str]]) -> Tuple[List[Dict[str, str]], List[str]]:
    from packaging.requirements import Requirement

    seen: Dict[str, Dict[str, str]] = {}
    missing: List[str] = []
    queue: List[Tuple[str, Set[str], str]] = [(name, extras, "direct") for name, extras in wanted.items()]
    while queue:
        name, extras, via = queue.pop()
        key = canonical(name)
        if key in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            if via == "direct":
                missing.append(name)
            continue
        licence = dist_licence(dist)
        seen[key] = {"name": dist.metadata["Name"], "version": dist.version, "licence": licence, "class": classify(licence), "via": via}
        for raw in dist.requires or []:
            try:
                req = Requirement(raw)
            except Exception:
                continue
            if req.marker is not None:
                envs = [{"extra": e} for e in extras] or [{"extra": ""}]
                if not any(req.marker.evaluate(e) for e in envs):
                    continue
            queue.append((req.name, set(req.extras), dist.metadata["Name"]))
    return sorted(seen.values(), key=lambda r: r["name"].lower()), sorted(missing)


# --- report -------------------------------------------------------------------------------------


def table(rows: List[Dict[str, str]], columns: Tuple[str, ...]) -> List[str]:
    out = ["| " + " | ".join(c.title() for c in columns) + " |", "|" + "---|" * len(columns)]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "")).replace("|", "/") for c in columns) + " |")
    return out


def render(python: List[Dict[str, str]], missing: List[str]) -> str:
    browser = [{"name": n, "version": v, "licence": l, "class": classify(l), "role": r} for n, v, l, r in BROWSER]
    counts = Counter(r["class"] for r in python + browser)
    flagged = [r for r in python + browser if r["class"] != "permissive"]
    lines = [
        "# Software components and licences",
        "",
        f"Generated by `scripts/license_inventory.py` on {date.today().isoformat()} from the installed packages. "
        "Re-run after any dependency change; CI can diff this file.",
        "",
        "## Summary",
        "",
        f"- Python packages (direct and transitive): **{len(python)}**",
        f"- Third-party files served to browsers: **{len(browser)}**",
        f"- Licence classes: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())),
        "- No component is licensed per user, per core, or per document: the software licence cost of the "
        "stack is zero; costs are hardware, hosting, support, and optional cloud AI usage (see sizing.md).",
        "",
        "## Needs legal review",
        "",
        "Copyleft components below are used unmodified, as libraries or as separate processes; none is "
        "distributed in modified form. Packages with no declared licence must be checked by hand.",
        "",
    ]
    flagged = [{**r, "note": NOTES.get(r["name"], NOTES.get(canonical(r["name"]), ""))} for r in flagged]
    lines += table(flagged, ("name", "version", "licence", "class", "note")) if flagged else ["None."]
    if missing:
        lines += ["", f"Not installed in the environment that produced this file (check on the worker image): {', '.join(missing)}."]
    lines += ["", "## Runtime services and AI models", ""]
    lines += table([{"component": c, "version": v, "licence": l, "class": classify(l), "role": r} for c, v, l, r in RUNTIME], ("component", "version", "licence", "class", "role"))
    lines += [
        "",
        "Redis 7.4 and later is not open source (RSALv2/SSPLv1). Use Redis 7.2 or Valkey 8 on premises; "
        "Upstash (the Railway setup) is a managed service and needs no licence.",
        "",
        "## Python packages",
        "",
    ]
    lines += table(python, ("name", "version", "licence", "class"))
    lines += ["", "## Browser assets (served from /static)", ""]
    lines += table(browser, ("name", "version", "licence", "class", "role"))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "evidence" / "licenses.md")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    python, missing = resolve_python(parse_requirements(ROOT / r for r in REQUIREMENTS))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(python, missing), encoding="utf-8")
    if args.json:
        args.json.write_text(json.dumps({"python": python, "missing": missing}, indent=2) + "\n", encoding="utf-8")
    flagged = sum(1 for r in python if r["class"] != "permissive")
    print(f"wrote {args.out}: {len(python)} Python, {flagged} flagged, {len(missing)} not installed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
