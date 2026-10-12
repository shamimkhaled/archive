#!/usr/bin/env python3
"""AI accuracy benchmarks for the evidence pack (post-demonstration report, sections 4.2, 7.2, and 8).

Each subcommand reads a JSONL manifest of labelled samples, runs the same code the product runs,
and writes a JSON report with accuracy, latency, failure cases, and correction effort. ``report``
turns the JSON files into the Markdown tables in docs/evidence/benchmark-results.md (interpreted in
benchmark-report.md), and
``compare`` puts a cloud run next to an on-premise run of the same manifest.

  ocr       Bangla/English OCR: CER (code points and grapheme clusters), WER, confidence routing
  stt       Speech-to-text: WER, CER, real-time factor, speaker count when diarization is on
  metadata  Classification (doc_type accuracy, macro-F1, confusion) and field extraction accuracy
  summary   Board briefing: heading detection, ROUGE-L against reference points, citation grounding
  ask       Grounded answers over HTTP against a running instance: refusal accuracy, citation
            precision/recall, required facts, restricted-document leaks, latency
  effort    Observed human correction effort from the audit log (verification, minutes, briefings)
  report    Markdown tables from one or more JSON reports
  compare   Metric deltas between two runs (for example OpenRouter vs vLLM on bank hardware)

Manifests (paths are relative to the manifest file; see eval/benchmarks/README.md):
  ocr:      {"id", "file", "page", "lang", "reference" | "reference_file", "quality"}
  stt:      {"id", "audio", "lang", "reference" | "reference_file", "speakers"}
  metadata: {"id", "file" | "text" | "text_file", "lang", "expected": {"doc_type", "title", ...}}
  summary:  {"id", "file" | "pages", "title", "sensitivity", "expected": {heading: [points] | "not_found"}}
  ask:      {"id", "question", "lang", "user", "expect_docs", "expect_answer", "should_refuse", "forbidden_docs"}

  PYTHONPATH=src python scripts/bench_accuracy.py ocr --manifest eval/benchmarks/ocr.jsonl --json eval/reports/ocr.json
  PYTHONPATH=src python scripts/bench_accuracy.py report eval/reports/*.json --out docs/evidence/benchmark-results.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bcp_project.evaluation import metrics as M  # noqa: E402

HEADINGS = (
    "subject",
    "background",
    "proposal",
    "financial_implications",
    "risks",
    "regulatory_considerations",
    "management_recommendation",
    "decision_required",
)
METADATA_FIELDS = ("title", "reference_number", "department", "subject", "category", "doc_date")


# --- shared -----------------------------------------------------------------------------------


def load_manifest(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip() and not line.lstrip().startswith("#"):
            row = json.loads(line)
            row.setdefault("id", f"{path.stem}-{line_no}")
            row["_base"] = str(path.parent)
            rows.append(row)
    return rows


def resolve(row: Dict[str, Any], key: str) -> Optional[Path]:
    value = row.get(key)
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else Path(row["_base"]) / path


def reference_text(row: Dict[str, Any], key: str = "reference") -> str:
    if row.get(key) is not None:
        return str(row[key])
    path = resolve(row, f"{key}_file")
    if path is None:
        raise ValueError(f"{row['id']}: needs '{key}' or '{key}_file'")
    return path.read_text(encoding="utf-8")


def provenance() -> Dict[str, Any]:
    from bcp_project import ai

    call = ai.last_call() or {}
    return {"provider": call.get("provider"), "model": call.get("model"), "route": call.get("source")}


def environment() -> Dict[str, Any]:
    keys = (
        "OCR_LANGUAGES",
        "OCR_REVIEW_THRESHOLD",
        "OCR_PAGE_REVIEW_THRESHOLD",
        "AI_CHAT_PROVIDER",
        "AI_CHAT_MODEL",
        "AI_STT_PROVIDER",
        "AI_STT_MODEL",
        "AI_EMBED_PROVIDER",
        "AI_EMBED_MODEL",
        "AI_EGRESS",
        "LOCAL_LLM_BASE_URL",
    )
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except OSError:
        commit = ""
    return {"commit": commit, "host": os.uname().nodename, **{k: os.getenv(k) for k in keys if os.getenv(k)}}


def write(report: Dict[str, Any], out: Optional[Path]) -> None:
    report.setdefault("generated_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    report.setdefault("environment", environment())
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}")
    print(json.dumps(report.get("summary"), indent=2, ensure_ascii=False, default=str))


def timed(fn: Callable[[], Any]) -> tuple:
    started = time.perf_counter()
    value = fn()
    return value, (time.perf_counter() - started) * 1000


def worst(rows: List[Dict[str, Any]], key: str, n: int = 10) -> List[Dict[str, Any]]:
    scored = [r for r in rows if r.get(key) is not None]
    return sorted(scored, key=lambda r: -r[key])[:n]


# --- OCR ----------------------------------------------------------------------------------------


def run_ocr(args) -> Dict[str, Any]:
    from bcp_project import ocr

    lang = args.ocr_lang or ocr.ocr_languages()
    _, page_threshold = ocr.review_thresholds()
    rows: List[Dict[str, Any]] = []
    for sample in load_manifest(args.manifest):
        path = resolve(sample, "file")
        data = path.read_bytes()
        page = int(sample.get("page") or 1)
        meta: Dict[str, Any] = {}
        if path.suffix.lower() == ".pdf" and not args.pipeline:
            result, ms = timed(lambda: ocr.ocr_image_detail(ocr._render_pdf_page(data, page), lang))
            text, conf, source = result.text, result.confidence, "ocr"
            meta = {"ink_coverage": result.ink_coverage, "recovered_lines": result.recovered_lines}
        else:
            if path.suffix.lower() == ".pdf":
                pages, ms = timed(lambda: ocr.extract_pages(data, "application/pdf"))
                hit = next((p for p in pages if p.page_number == page), None)
            else:
                pages, ms = timed(lambda: ocr.extract_image(data))
                hit = pages[page - 1] if len(pages) >= page else None
            text, conf, source = (hit.text, hit.confidence, hit.source) if hit else ("", 0.0, "missing")
            meta = {k: v for k, v in (hit.metadata if hit else {}).items() if k in ("ink_coverage", "recovered_lines")}
        reference = reference_text(sample)
        cp, gc, w = M.cer(reference, text), M.cer(reference, text, clusters=True), M.wer(reference, text)
        rows.append(
            {
                "id": sample["id"],
                "lang": sample.get("lang") or M.language_of(reference),
                "quality": sample.get("quality") or "unspecified",
                "source": source,
                "confidence": conf,
                "cer": cp.rate,
                "cer_graphemes": gc.rate,
                "wer": w.rate,
                "_cer": cp,
                "_cer_g": gc,
                "_wer": w,
                "edits": cp.distance,
                "ref_chars": cp.reference_length,
                "latency_ms": round(ms, 1),
                "flagged_for_review": conf is not None and conf < page_threshold,
                "correction_minutes_est": M.correction_minutes(gc.distance, edits_per_minute=args.edits_per_minute),
                **meta,
            }
        )
        print(f"{sample['id']}: CER {cp.rate:.3f}  WER {w.rate:.3f}  conf {conf}  {ms:.0f} ms")

    def group(selected: List[Dict[str, Any]]) -> Dict[str, Any]:
        high_error = [r for r in selected if (r["cer"] or 0) > args.high_cer]
        return {
            "pages": len(selected),
            "cer": M.pooled_rate(r["_cer"] for r in selected),
            "cer_graphemes": M.pooled_rate(r["_cer_g"] for r in selected),
            "wer": M.pooled_rate(r["_wer"] for r in selected),
            "mean_confidence": M.mean(r["confidence"] for r in selected),
            "latency_ms": M.latency_stats(r["latency_ms"] for r in selected),
            "correction_minutes_est_per_page": M.mean(r["correction_minutes_est"] for r in selected),
            "review_routing": {
                "high_error_pages": len(high_error),
                "high_error_caught": sum(1 for r in high_error if r["flagged_for_review"]),
                "flagged_pages": sum(1 for r in selected if r["flagged_for_review"]),
            },
        }

    overall = group(rows)
    by_lang = {lang_: group([r for r in rows if r["lang"] == lang_]) for lang_ in sorted({r["lang"] for r in rows})}
    by_quality = {q: group([r for r in rows if r["quality"] == q]) for q in sorted({r["quality"] for r in rows})}
    for r in rows:
        for key in ("_cer", "_cer_g", "_wer"):
            r.pop(key)
    return {
        "kind": "ocr",
        "config": {"ocr_languages": lang, "mode": "pipeline" if args.pipeline else "force_ocr", "high_cer": args.high_cer},
        "summary": {"all": overall, "by_lang": by_lang},
        "by_quality": by_quality,
        "failures": worst(rows, "cer"),
        "rows": rows,
    }


# --- STT ----------------------------------------------------------------------------------------


def audio_seconds(path: Path) -> Optional[float]:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        return float(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def run_stt(args) -> Dict[str, Any]:
    from bcp_project import ai

    rows: List[Dict[str, Any]] = []
    stats: Dict[str, List[M.EditStats]] = {}
    for sample in load_manifest(args.manifest):
        path = resolve(sample, "audio")
        lang = sample.get("lang")
        segments, ms = timed(lambda: ai.transcribe(str(path), language=lang, sensitivity=args.sensitivity))
        hypothesis = " ".join(str(s.get("text") or "") for s in segments)
        reference = reference_text(sample)
        w, c = M.wer(reference, hypothesis), M.cer(reference, hypothesis)
        seconds = audio_seconds(path)
        speakers = {s.get("speaker") for s in segments if s.get("speaker")}
        key = lang or M.language_of(reference)
        stats.setdefault(f"{key}:wer", []).append(w)
        stats.setdefault(f"{key}:cer", []).append(c)
        rows.append(
            {
                "id": sample["id"],
                "lang": key,
                "condition": sample.get("condition") or "unspecified",
                "wer": w.rate,
                "cer": c.rate,
                "audio_s": seconds,
                "processing_s": round(ms / 1000, 2),
                "rtf": round(ms / 1000 / seconds, 3) if seconds else None,
                "speakers_expected": sample.get("speakers"),
                "speakers_found": len(speakers) or None,
                **provenance(),
            }
        )
        print(f"{sample['id']}: WER {w.rate:.3f}  CER {c.rate:.3f}  RTF {rows[-1]['rtf']}")
    langs = sorted({r["lang"] for r in rows})
    by_lang = {
        lang_: {
            "files": sum(1 for r in rows if r["lang"] == lang_),
            "wer": M.pooled_rate(stats[f"{lang_}:wer"]),
            "cer": M.pooled_rate(stats[f"{lang_}:cer"]),
            "rtf_mean": M.mean(r["rtf"] for r in rows if r["lang"] == lang_),
            "audio_minutes": round(sum(r["audio_s"] or 0 for r in rows if r["lang"] == lang_) / 60, 1),
        }
        for lang_ in langs
    }
    with_speakers = [r for r in rows if r["speakers_expected"]]
    return {
        "kind": "stt",
        "summary": {
            "by_lang": by_lang,
            "speaker_count_exact": (
                round(sum(1 for r in with_speakers if r["speakers_found"] == r["speakers_expected"]) / len(with_speakers), 4)
                if with_speakers
                else None
            ),
        },
        "failures": worst(rows, "wer"),
        "rows": rows,
    }


# --- classification and metadata ----------------------------------------------------------------


def document_text(sample: Dict[str, Any]) -> str:
    if sample.get("text"):
        return str(sample["text"])
    text_file = resolve(sample, "text_file")
    if text_file:
        return text_file.read_text(encoding="utf-8")
    from bcp_project import ocr

    path = resolve(sample, "file")
    content_type = "application/pdf" if path.suffix.lower() == ".pdf" else "image/png"
    return "\n\n".join(p.text for p in ocr.extract_pages(path.read_bytes(), content_type))


def run_metadata(args) -> Dict[str, Any]:
    from bcp_project.document_types import normalize_document_type
    from bcp_project.services.metadata_suggest import suggest_metadata

    rows: List[Dict[str, Any]] = []
    for sample in load_manifest(args.manifest):
        expected = sample.get("expected") or {}
        text = document_text(sample)
        suggested, ms = timed(lambda: suggest_metadata(text, sensitivity=args.sensitivity) or {})
        fields = {}
        for name in METADATA_FIELDS:
            if name in expected:
                fields[name] = M.field_score(expected[name], suggested.get(name), field=name)
        row = {
            "id": sample["id"],
            "lang": sample.get("lang") or M.language_of(text),
            "doc_type_expected": normalize_document_type(str(expected.get("doc_type") or "")),
            "doc_type_predicted": normalize_document_type(str(suggested.get("doc_type") or "")),
            "fields": fields,
            "suggested": {k: v for k, v in suggested.items() if k != "_source"},
            "source": (suggested.get("_source") or {}).get("kind"),
            "latency_ms": round(ms, 1),
            **provenance(),
        }
        rows.append(row)
        print(f"{sample['id']}: {row['doc_type_expected']} -> {row['doc_type_predicted']}")
    labelled = [r for r in rows if r["doc_type_expected"]]
    classification = M.classification_report([r["doc_type_expected"] for r in labelled], [r["doc_type_predicted"] for r in labelled])
    field_summary = {}
    for name in METADATA_FIELDS:
        scored = [r["fields"][name] for r in rows if name in r["fields"]]
        if scored:
            field_summary[name] = {
                "n": len(scored),
                "exact": round(sum(s["exact"] for s in scored) / len(scored), 4),
                "fuzzy": round(sum(s["fuzzy"] for s in scored) / len(scored), 4),
            }
    for r in rows:
        r["misclassified"] = bool(r["doc_type_expected"]) and r["doc_type_expected"] != r["doc_type_predicted"]
    return {
        "kind": "metadata",
        "summary": {
            "classification": {k: classification[k] for k in ("n", "accuracy", "macro_f1")},
            "fields": field_summary,
            "rule_fallbacks": sum(1 for r in rows if r["source"] == "rules"),
            "latency_ms": M.latency_stats(r["latency_ms"] for r in rows),
        },
        "classification": classification,
        "failures": [r for r in rows if r["misclassified"]][:10],
        "rows": rows,
    }


# --- Board briefing ----------------------------------------------------------------------------


def paper_pages(sample: Dict[str, Any]) -> List[tuple]:
    if sample.get("pages"):
        return [(i, str(t)) for i, t in enumerate(sample["pages"], 1)]
    from bcp_project import ocr

    path = resolve(sample, "file")
    return [(p.page_number, p.text) for p in ocr.extract_pages(path.read_bytes(), "application/pdf")]


def run_summary(args) -> Dict[str, Any]:
    from bcp_project.services.briefing import generate_briefing

    rows: List[Dict[str, Any]] = []
    for sample in load_manifest(args.manifest):
        pages = paper_pages(sample)
        (summary, prov), ms = timed(
            lambda: generate_briefing(pages, sensitivity=sample.get("sensitivity") or args.sensitivity, title=sample.get("title") or "")
        )
        expected = sample.get("expected") or {}
        headings = {}
        tp = fp = fn = 0
        for key in HEADINGS:
            section = summary.section(key)
            found = bool(section.points)
            want = expected.get(key)
            if want is None:
                continue
            should_find = want != "not_found" and bool(want)
            tp += found and should_find
            fp += found and not should_find
            fn += (not found) and should_find
            text = " ".join(p.text for p in section.points)
            headings[key] = {
                "found": found,
                "expected": should_find,
                "rouge_l": M.rouge_l(text, " ".join(want))["f1"] if should_find and isinstance(want, list) else None,
                "pages_cited": sorted({p.page for p in section.points if p.page}),
            }
        cites = prov.get("citations") or {}
        proposed = cites.get("proposed") or 0
        rows.append(
            {
                "id": sample["id"],
                "pages": len(pages),
                "headings": headings,
                "heading_precision": round(tp / (tp + fp), 4) if tp + fp else None,
                "heading_recall": round(tp / (tp + fn), 4) if tp + fn else None,
                "rouge_l": M.mean(h["rouge_l"] for h in headings.values()),
                "citations_proposed": proposed,
                "citations_dropped_ungrounded": cites.get("dropped_ungrounded") or 0,
                "grounded_rate": round(1 - (cites.get("dropped_ungrounded") or 0) / proposed, 4) if proposed else None,
                "windows": (prov.get("map_reduce") or {}).get("windows"),
                "latency_ms": round(ms, 1),
                "provider": prov.get("provider"),
                "model": prov.get("model"),
            }
        )
        print(f"{sample['id']}: heading recall {rows[-1]['heading_recall']}  ROUGE-L {rows[-1]['rouge_l']}  {ms / 1000:.1f}s")
    return {
        "kind": "summary",
        "summary": {
            "papers": len(rows),
            "heading_precision": M.mean(r["heading_precision"] for r in rows),
            "heading_recall": M.mean(r["heading_recall"] for r in rows),
            "rouge_l": M.mean(r["rouge_l"] for r in rows),
            "citation_grounded_rate": M.mean(r["grounded_rate"] for r in rows),
            "latency_ms": M.latency_stats(r["latency_ms"] for r in rows),
        },
        "failures": sorted(rows, key=lambda r: (r["heading_recall"] or 0))[:10],
        "rows": rows,
    }


# --- Ask (HTTP) --------------------------------------------------------------------------------


def run_ask(args) -> Dict[str, Any]:
    import httpx

    tokens: Dict[str, str] = json.loads(os.getenv("BENCH_TOKENS") or "{}")
    if args.token:
        tokens.setdefault("default", args.token)
    rows: List[Dict[str, Any]] = []
    with httpx.Client(base_url=args.base_url, timeout=args.timeout) as client:
        for sample in load_manifest(args.manifest):
            user = sample.get("user") or "default"
            token = tokens.get(user)
            if not token:
                raise SystemExit(f"No bearer token for user '{user}' (set BENCH_TOKENS='{{\"{user}\": \"...\"}}')")
            started = time.perf_counter()
            response = client.post(
                "/api/v1/ask",
                json={"question": sample["question"], "lang": sample.get("lang"), "limit": 8},
                headers={"Authorization": f"Bearer {token}"},
            )
            ms = (time.perf_counter() - started) * 1000
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            cited = [c.get("doc_id") for c in body.get("citations") or []]
            considered = [e.get("doc_id") for e in body.get("evidence") or []]
            should_refuse = bool(sample.get("should_refuse"))
            refused = bool(body.get("refused")) or response.status_code >= 400
            facts = sample.get("expect_answer") or []
            facts_found = [f for f in facts if M.contains_fact(body.get("answer") or "", f)]
            forbidden = set(sample.get("forbidden_docs") or [])
            leaked = sorted(forbidden & (set(cited) | set(considered)))
            pr = M.set_precision_recall(cited, sample.get("expect_docs") or [])
            rows.append(
                {
                    "id": sample["id"],
                    "lang": sample.get("lang") or M.language_of(sample["question"]),
                    "user": user,
                    "status": response.status_code,
                    "should_refuse": should_refuse,
                    "refused": refused,
                    "refusal_correct": refused == should_refuse,
                    "citation_precision": pr["precision"] if not should_refuse else None,
                    "citation_recall": pr["recall"] if not should_refuse else None,
                    "facts_expected": len(facts),
                    "facts_found": len(facts_found),
                    "leaked_restricted": leaked,
                    "latency_ms": round(ms, 1),
                    "model": (body.get("answered_by") or {}).get("model"),
                }
            )
            print(f"{sample['id']}: refused={refused} (want {should_refuse}) cites={cited} {ms:.0f} ms")
    answerable = [r for r in rows if not r["should_refuse"]]
    fact_total = sum(r["facts_expected"] for r in answerable)
    return {
        "kind": "ask",
        "config": {"base_url": args.base_url},
        "summary": {
            "questions": len(rows),
            "refusal_accuracy": round(sum(r["refusal_correct"] for r in rows) / len(rows), 4) if rows else None,
            "answered_when_answerable": round(sum(not r["refused"] for r in answerable) / len(answerable), 4) if answerable else None,
            "citation_precision": M.mean(r["citation_precision"] for r in answerable if not r["refused"]),
            "citation_recall": M.mean(r["citation_recall"] for r in answerable if not r["refused"]),
            "fact_recall": round(sum(r["facts_found"] for r in answerable) / fact_total, 4) if fact_total else None,
            "restricted_leaks": sum(1 for r in rows if r["leaked_restricted"]),
            "latency_ms": M.latency_stats(r["latency_ms"] for r in rows),
        },
        "failures": [r for r in rows if not r["refusal_correct"] or r["leaked_restricted"] or (r["facts_found"] < r["facts_expected"])][:10],
        "rows": rows,
    }


# --- Observed effort ---------------------------------------------------------------------------


def run_effort(args) -> Dict[str, Any]:
    from sqlalchemy import select

    from bcp_project.db import AsyncSessionLocal
    from bcp_project.evaluation.effort import correction_effort
    from bcp_project.models import AuditLog

    actions = (
        "verify_open",
        "verify_approve",
        "verify_reject",
        "metadata_confirm",
        "minutes_save",
        "minutes_approve",
        "briefing_generate",
        "briefing_edit",
        "briefing_approve",
    )

    async def fetch() -> List[Dict[str, Any]]:
        async with AsyncSessionLocal() as db:
            query = select(AuditLog.id, AuditLog.username, AuditLog.action, AuditLog.resource_id, AuditLog.detail, AuditLog.created_at).where(
                AuditLog.action.in_(actions)
            )
            if args.since:
                query = query.where(AuditLog.created_at >= datetime.fromisoformat(args.since))
            return [dict(r._mapping) for r in (await db.execute(query)).all()]

    rows = asyncio.run(fetch())
    return {"kind": "effort", "config": {"since": args.since, "events": len(rows)}, "summary": correction_effort(rows)}


# --- report / compare ----------------------------------------------------------------------------


def _fmt(value: Any, pct: bool = False) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value * 100:.1f}%" if pct else f"{value:,.2f}"
    return str(value)


def markdown(report: Dict[str, Any]) -> str:
    kind, s = report.get("kind"), report.get("summary") or {}
    env = report.get("environment") or {}
    lines = [f"### {kind.upper()} — {report.get('generated_at', '')}", "", f"Commit `{env.get('commit', '?')}` on `{env.get('host', '?')}`.", ""]
    if kind == "ocr":
        lines += ["| Language | Pages | CER | CER (graphemes) | WER | Mean confidence | p95 latency (ms) | Est. correction min/page | High-error pages caught by review |", "|---|---|---|---|---|---|---|---|---|"]
        for lang, g in s.get("by_lang", {}).items():
            routing = g["review_routing"]
            caught = f"{routing['high_error_caught']}/{routing['high_error_pages']}"
            lines.append(
                f"| {lang} | {g['pages']} | {_fmt(g['cer'], True)} | {_fmt(g['cer_graphemes'], True)} | {_fmt(g['wer'], True)} | "
                f"{_fmt(g['mean_confidence'])} | {_fmt(g['latency_ms']['p95'])} | {_fmt(g['correction_minutes_est_per_page'])} | {caught} |"
            )
        if report.get("by_quality"):
            lines += ["", "| Scan quality | Pages | CER | WER |", "|---|---|---|---|"]
            for q, g in report["by_quality"].items():
                lines.append(f"| {q} | {g['pages']} | {_fmt(g['cer'], True)} | {_fmt(g['wer'], True)} |")
    elif kind == "stt":
        lines += ["| Language | Files | Audio (min) | WER | CER | Mean RTF |", "|---|---|---|---|---|---|"]
        for lang, g in s.get("by_lang", {}).items():
            lines.append(f"| {lang} | {g['files']} | {g['audio_minutes']} | {_fmt(g['wer'], True)} | {_fmt(g['cer'], True)} | {_fmt(g['rtf_mean'])} |")
        if s.get("speaker_count_exact") is not None:
            lines.append(f"\nSpeaker count correct: {_fmt(s['speaker_count_exact'], True)}.")
    elif kind == "metadata":
        c = s.get("classification") or {}
        lines += [f"Classification: {c.get('n')} documents, accuracy {_fmt(c.get('accuracy'), True)}, macro-F1 {_fmt(c.get('macro_f1'))}.", ""]
        lines += ["| Field | Documents | Exact | Fuzzy (≥90% similar) |", "|---|---|---|---|"]
        for name, f in s.get("fields", {}).items():
            lines.append(f"| {name} | {f['n']} | {_fmt(f['exact'], True)} | {_fmt(f['fuzzy'], True)} |")
        lines.append(f"\nRule-based fallbacks (model unavailable): {s.get('rule_fallbacks', 0)}. p95 latency {_fmt((s.get('latency_ms') or {}).get('p95'))} ms.")
    elif kind == "summary":
        lines += [
            "| Papers | Heading precision | Heading recall | ROUGE-L F1 | Citations kept after grounding | p95 latency (s) |",
            "|---|---|---|---|---|---|",
            f"| {s.get('papers')} | {_fmt(s.get('heading_precision'), True)} | {_fmt(s.get('heading_recall'), True)} | {_fmt(s.get('rouge_l'))} | "
            f"{_fmt(s.get('citation_grounded_rate'), True)} | {_fmt(((s.get('latency_ms') or {}).get('p95') or 0) / 1000)} |",
        ]
    elif kind == "ask":
        lines += [
            "| Questions | Refusal accuracy | Answered (answerable) | Citation precision | Citation recall | Fact recall | Restricted leaks | p50 / p95 latency (ms) |",
            "|---|---|---|---|---|---|---|---|",
            f"| {s.get('questions')} | {_fmt(s.get('refusal_accuracy'), True)} | {_fmt(s.get('answered_when_answerable'), True)} | "
            f"{_fmt(s.get('citation_precision'), True)} | {_fmt(s.get('citation_recall'), True)} | {_fmt(s.get('fact_recall'), True)} | "
            f"{s.get('restricted_leaks')} | {_fmt((s.get('latency_ms') or {}).get('p50'))} / {_fmt((s.get('latency_ms') or {}).get('p95'))} |",
        ]
    elif kind == "effort":
        v, mi, b = s.get("ocr_verification", {}), s.get("minutes", {}), s.get("briefings", {})
        lines += [
            "| Workflow | Completed | Median | p90 | Unit |",
            "|---|---|---|---|---|",
            f"| OCR verification | {v.get('approved', 0) + v.get('rejected', 0)} | {_fmt(v['minutes_per_document']['median'])} | {_fmt(v['minutes_per_document']['p90'])} | minutes per document |",
            f"| OCR pages corrected | {v.get('approved', 0)} | {_fmt(v['pages_edited_per_document']['median'])} | {_fmt(v['pages_edited_per_document']['p90'])} | pages per document |",
            f"| Minutes | {mi.get('approved', 0)} | {_fmt(mi['saves_before_approval']['median'])} | {_fmt(mi['saves_before_approval']['p90'])} | saves before approval |",
            f"| Board briefings | {b.get('approved', 0)} | {_fmt(b['section_edits_before_approval']['median'])} | {_fmt(b['section_edits_before_approval']['p90'])} | section edits before approval |",
        ]
    elif kind == "retrieval":
        lines += ["| Slice | Queries | Recall@10 | MRR | Grounded | Cited page correct |", "|---|---|---|---|---|---|"]
        for name in ("overall", "en", "bn"):
            g = report.get(name) or {}
            if g.get("n"):
                lines.append(
                    f"| {name} | {g['n']} | {_fmt(g['recall_at_10'], True)} | {_fmt(g['mrr'])} | {_fmt(g['grounding'], True)} | {_fmt(g['page_accuracy'], True)} |"
                )
        latency = report.get("latency_ms") or {}
        lines.append(
            f"\nRestricted-document leaks: {len(report.get('acl_leaks') or [])}. Filter violations: {len(report.get('filter_violations') or [])}. "
            f"Latency p50/p95: {latency.get('p50')} / {latency.get('p95')} ms."
        )
        return "\n".join(lines) + "\n"
    failures = report.get("failures") or []
    if failures and isinstance(failures[0], dict):
        lines += ["", f"Failure cases ({len(failures)} worst): " + ", ".join(f"`{f.get('id')}`" for f in failures[:10]) + "."]
    return "\n".join(lines) + "\n"


def run_report(args) -> None:
    parts = ["# Benchmark results", "", "Generated by `scripts/bench_accuracy.py report`. Methodology: docs/evidence/benchmark-report.md.", ""]
    for item in args.inputs:
        path, _, label = item.partition("=")
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        report.setdefault("kind", "retrieval" if "overall" in report and "acl_leaks" in report else Path(path).stem)
        report.setdefault("generated_at", datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        text = markdown(report)
        if label:
            text = text.replace(f"### {report['kind'].upper()} —", f"### {report['kind'].upper()}: {label} —", 1)
        parts.append(text)
    text = "\n".join(parts)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)


def _flatten(prefix: str, value: Any, out: Dict[str, float]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}.{k}" if prefix else k, v, out)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out[prefix] = float(value)


def run_compare(args) -> None:
    a = json.loads(args.baseline.read_text(encoding="utf-8"))
    b = json.loads(args.candidate.read_text(encoding="utf-8"))
    fa: Dict[str, float] = {}
    fb: Dict[str, float] = {}
    _flatten("", a.get("summary"), fa)
    _flatten("", b.get("summary"), fb)
    label_a = args.baseline_label or (a.get("environment") or {}).get("AI_CHAT_PROVIDER") or "baseline"
    label_b = args.candidate_label or (b.get("environment") or {}).get("AI_CHAT_PROVIDER") or "candidate"
    lines = [f"### {a.get('kind', '').upper()}: {label_a} vs {label_b}", "", f"| Metric | {label_a} | {label_b} | Change |", "|---|---|---|---|"]
    for key in sorted(set(fa) & set(fb)):
        delta = fb[key] - fa[key]
        lines.append(f"| {key} | {fa[key]:.4g} | {fb[key]:.4g} | {delta:+.4g} |")
    text = "\n".join(lines) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def bench(name: str, **extra) -> argparse.ArgumentParser:
        p = sub.add_parser(name)
        p.add_argument("--manifest", type=Path, required=name != "effort")
        p.add_argument("--json", type=Path, help="Write the report here")
        p.add_argument("--sensitivity", default="internal", help="Sensitivity label passed to the AI egress policy")
        return p

    p = bench("ocr")
    p.add_argument("--ocr-lang", help="Tesseract languages (default: OCR_LANGUAGES or ben+eng)")
    p.add_argument("--pipeline", action="store_true", help="Use the ingest path (text layer when present) instead of forcing OCR")
    p.add_argument("--high-cer", type=float, default=0.05, help="CER above which a page should go to human review")
    p.add_argument("--edits-per-minute", type=float, default=30.0)
    bench("stt")
    bench("metadata")
    bench("summary")
    p = bench("ask")
    p.add_argument("--base-url", default=os.getenv("BENCH_BASE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--token", default=os.getenv("BENCH_TOKEN"), help="Bearer token used when a sample has no 'user'")
    p.add_argument("--timeout", type=float, default=120.0)
    p = bench("effort")
    p.add_argument("--since", help="ISO date; only audit events after this")
    p = sub.add_parser("report")
    p.add_argument("inputs", nargs="+", help="JSON reports, optionally path=Label")
    p.add_argument("--out", type=Path)
    p = sub.add_parser("compare")
    p.add_argument("baseline", type=Path)
    p.add_argument("candidate", type=Path)
    p.add_argument("--baseline-label")
    p.add_argument("--candidate-label")
    p.add_argument("--out", type=Path)
    args = parser.parse_args()

    if args.command == "report":
        run_report(args)
        return 0
    if args.command == "compare":
        run_compare(args)
        return 0

    from bcp_project.config import load_environment

    load_environment()
    runner = {"ocr": run_ocr, "stt": run_stt, "metadata": run_metadata, "summary": run_summary, "ask": run_ask, "effort": run_effort}[args.command]
    write(runner(args), args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
