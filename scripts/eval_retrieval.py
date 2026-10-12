#!/usr/bin/env python3
"""Bilingual retrieval evaluation: recall@10, MRR, answer grounding, ACL leaks, filter violations.

Indexes ``eval/corpus_bilingual.json`` into an in-memory Qdrant through the production
indexing helpers, then runs every query in ``eval/retrieval_bilingual.jsonl`` through
the production search path (ACL + filters inside the vector query, fusion, rerank,
grounded passages). Nothing touches Postgres, Redis, or a remote Qdrant.

By default it uses the stub embedder (hashed bag of words) and the lexical reranker, so
CI measures the pipeline rather than a model. ``--live`` keeps the configured
AI_EMBED_* / AI_RERANK_* providers to compare real models on the same set.

Example:
  PYTHONPATH=src python scripts/eval_retrieval.py
  PYTHONPATH=src python scripts/eval_retrieval.py --live --json eval/last_run.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_CORPUS = ROOT / "eval" / "corpus_bilingual.json"
DEFAULT_QUERIES = ROOT / "eval" / "retrieval_bilingual.jsonl"
K = 10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bilingual retrieval evaluation")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--live", action="store_true", help="Use the configured embedding and rerank providers")
    parser.add_argument("--min-recall", type=float, default=float(os.getenv("EVAL_MIN_RECALL", "0.85")))
    parser.add_argument("--min-mrr", type=float, default=float(os.getenv("EVAL_MIN_MRR", "0.70")))
    parser.add_argument("--min-grounding", type=float, default=float(os.getenv("EVAL_MIN_GROUNDING", "0.80")))
    parser.add_argument("--json", type=Path, help="Write the full report here")
    parser.add_argument("--verbose", action="store_true", help="Print every miss")
    return parser.parse_args()


def configure_offline() -> None:
    os.environ["AI_EMBED_PROVIDER"] = "stub"
    os.environ["AI_EMBED_MODEL"] = "stub-embed"
    os.environ["AI_EMBED_DIM"] = "256"
    os.environ["AI_RERANK_PROVIDER"] = "stub"
    os.environ.pop("AI_RERANK_MODEL", None)
    os.environ["AI_EGRESS"] = "internal_only"


def load_corpus(path: Path) -> List[Dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_queries(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def index_corpus(corpus: List[Dict[str, Any]]) -> None:
    from bcp_project.jobs.handlers import run_chunk_index
    from bcp_project.qdrant_store import make_qdrant_indexer
    from bcp_project.search_acl import search_payload
    from bcp_project.services.ingest import index_summary_vector

    qdrant = make_qdrant_indexer()
    qdrant.create_collections()
    for doc in corpus:
        record = SimpleNamespace(**{**doc, "doc_date": date.fromisoformat(doc["doc_date"]), "current_version": 1})
        acl = search_payload(record)
        summary = {
            "doc_id": doc["doc_id"],
            "doc_type": doc["doc_type"],
            "doc_date": doc["doc_date"],
            "searchable_keywords": doc["keywords"],
            "major_projects": [],
            "core_info": {"organization": "Sonali Bank PLC", "meeting_title": doc["title"], "date": doc["doc_date"]},
            "key_personnel": [],
            "finance_and_admin": [],
        }
        index_summary_vector(qdrant, doc["doc_id"], {**summary, **acl}, doc["sensitivity"])
        pages = [
            {"text": text, "metadata": {**acl, "page_number": n, "text_source": "text"}}
            for n, text in enumerate(doc["pages"], start=1)
        ]
        run_chunk_index(doc["doc_id"], pages, sensitivity=doc["sensitivity"])


def matches_filters(doc: Dict[str, Any], filters: Dict[str, str]) -> bool:
    from bcp_project.search_acl import norm

    if filters.get("department") and norm(doc["department"]) != norm(filters["department"]):
        return False
    if filters.get("category") and norm(doc["category"]) != norm(filters["category"]):
        return False
    if filters.get("date_from") and doc["doc_date"] < filters["date_from"]:
        return False
    if filters.get("date_to") and doc["doc_date"] > filters["date_to"]:
        return False
    return True


async def search(q: str, acl, filters, lang: str, docs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The vector half of ``services.search.hybrid_search``; metadata comes from the corpus, not Postgres."""
    from bcp_project.services.search import RERANK_CANDIDATES, _rerank, _rerank_text, _vector_hits

    started = time.perf_counter()
    candidates = max(K, RERANK_CANDIDATES)
    qdrant, summary_rows, chunk_best, ok = await _vector_hits(q, acl, filters, candidates)
    rows: List[Dict[str, Any]] = []
    if qdrant is not None and (summary_rows or chunk_best):
        rows = qdrant.fuse_hybrid_results(summary_rows, chunk_best, q, limit=candidates, lang=lang, use_chunks=True)
    for row in rows:
        doc = docs.get(row.get("doc_id")) or {}
        row.update(title=doc.get("title"), sensitivity=doc.get("sensitivity"))
    reranked = await _rerank(q, rows, _rerank_text)
    return {"rows": rows[:K], "ok": ok, "reranked": reranked, "ms": (time.perf_counter() - started) * 1000}


async def evaluate(corpus: List[Dict[str, Any]], queries: List[Dict[str, Any]], verbose: bool) -> Dict[str, Any]:
    from bcp_project.search_acl import AclScope, PRIVILEGED_ROLES, SearchFilters
    from bcp_project.services.search import grounded_passages

    docs = {d["doc_id"]: d for d in corpus}
    restricted = {d["doc_id"] for d in corpus if d["sensitivity"] == "restricted"}
    member = AclScope(unrestricted=False, role="board_member")

    per_query: List[Dict[str, Any]] = []
    latencies: List[float] = []
    acl_leaks: List[str] = []
    filter_violations: List[str] = []

    for item in queries:
        role = item.get("role", "board_member")
        acl = AclScope(unrestricted=True, role=role) if role in PRIVILEGED_ROLES else AclScope(unrestricted=False, role=role)
        filter_params = item.get("filters") or {}
        filters = SearchFilters.from_params(**filter_params)
        result = await search(item["q"], acl, filters, item["lang"], docs)
        latencies.append(result["ms"])
        ranked = [r.get("doc_id") for r in result["rows"]]
        relevant = set(item["relevant"])

        hits = [doc_id for doc_id in ranked if doc_id in relevant]
        recall = len(set(hits)) / len(relevant)
        first = next((i for i, doc_id in enumerate(ranked, start=1) if doc_id in relevant), None)
        rr = 1.0 / first if first else 0.0

        for doc_id in ranked:
            if filter_params and doc_id in docs and not matches_filters(docs[doc_id], filter_params):
                filter_violations.append(f"{item['id']}:{doc_id}")

        passages = await grounded_passages(acl, item["q"], ranked[:5], k=4)
        answer = item["answer"].casefold()
        grounded = any(answer in p["text"].casefold() and p["doc_id"] in relevant for p in passages)
        page_ok = any(p["doc_id"] in relevant and p.get("page") == item.get("page") for p in passages)

        if not acl.unrestricted:
            acl_leaks.extend(f"{item['id']}:{d}" for d in ranked if d in restricted)

        per_query.append(
            {
                "id": item["id"],
                "lang": item["lang"],
                "recall": recall,
                "rr": rr,
                "rank": first,
                "grounded": grounded,
                "page_ok": page_ok,
                "reranked": result["reranked"],
            }
        )
        if verbose and (recall < 1 or not grounded):
            print(f"  miss {item['id']} [{item['lang']}] rank={first} grounded={grounded} top={ranked[:3]}")

    # Every query again as a board member: restricted documents must never surface.
    for item in queries:
        result = await search(item["q"], member, SearchFilters(), item["lang"], docs)
        acl_leaks.extend(f"{item['id']}(member):{r.get('doc_id')}" for r in result["rows"] if r.get("doc_id") in restricted)
        passages = await grounded_passages(member, item["q"], list(restricted), k=4)
        acl_leaks.extend(f"{item['id']}(member-passage):{p['doc_id']}" for p in passages)

    def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not rows:
            return {"n": 0}
        return {
            "n": len(rows),
            "recall_at_10": round(statistics.mean(r["recall"] for r in rows), 4),
            "mrr": round(statistics.mean(r["rr"] for r in rows), 4),
            "grounding": round(statistics.mean(1.0 if r["grounded"] else 0.0 for r in rows), 4),
            "page_accuracy": round(statistics.mean(1.0 if r["page_ok"] else 0.0 for r in rows), 4),
        }

    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))] if ordered else 0.0
    return {
        "overall": summarize(per_query),
        "en": summarize([r for r in per_query if r["lang"] == "en"]),
        "bn": summarize([r for r in per_query if r["lang"] == "bn"]),
        "reranked_share": round(statistics.mean(1.0 if r["reranked"] else 0.0 for r in per_query), 4) if per_query else 0.0,
        "latency_ms": {"p50": round(statistics.median(latencies), 1) if latencies else 0.0, "p95": round(p95, 1)},
        "acl_leaks": acl_leaks,
        "filter_violations": filter_violations,
        "queries": per_query,
    }


def markdown(report: Dict[str, Any], gates: Dict[str, float], failures: List[str], mode: str) -> str:
    lines = [
        f"### Bilingual retrieval evaluation ({mode})",
        "",
        "| Slice | Queries | Recall@10 | MRR | Grounding | Cited page |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name in ("overall", "en", "bn"):
        s = report[name]
        if s.get("n"):
            lines.append(
                f"| {name} | {s['n']} | {s['recall_at_10']:.3f} | {s['mrr']:.3f} | {s['grounding']:.3f} | {s['page_accuracy']:.3f} |"
            )
    lines += [
        "",
        f"ACL leaks: **{len(report['acl_leaks'])}** · Filter violations: **{len(report['filter_violations'])}** · "
        f"Reranked: {report['reranked_share']:.0%} · Latency p50/p95 (in-memory): "
        f"{report['latency_ms']['p50']} / {report['latency_ms']['p95']} ms",
        "",
        f"Gates: recall ≥ {gates['recall']}, MRR ≥ {gates['mrr']}, grounding ≥ {gates['grounding']}, zero ACL leaks, zero filter violations.",
        "",
        "**Result: " + ("FAIL** — " + "; ".join(failures) if failures else "PASS**"),
    ]
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    if not args.live:
        configure_offline()

    from bcp_project.config import load_environment

    load_environment()
    if not args.live:
        configure_offline()
    for name in ("", "bcp_project"):
        logging.getLogger(name).setLevel(logging.ERROR)

    import bcp_project.ports.adapters as adapters
    import bcp_project.qdrant_store as qdrant_store

    qdrant_store._indexer = qdrant_store.QdrantIndexer(url=":memory:")
    adapters._vector = None

    corpus = load_corpus(args.corpus)
    queries = load_queries(args.queries)
    index_corpus(corpus)
    report = asyncio.run(evaluate(corpus, queries, args.verbose))

    gates = {"recall": args.min_recall, "mrr": args.min_mrr, "grounding": args.min_grounding}
    overall = report["overall"]
    failures = []
    if overall["recall_at_10"] < gates["recall"]:
        failures.append(f"recall@10 {overall['recall_at_10']} < {gates['recall']}")
    if overall["mrr"] < gates["mrr"]:
        failures.append(f"MRR {overall['mrr']} < {gates['mrr']}")
    if overall["grounding"] < gates["grounding"]:
        failures.append(f"grounding {overall['grounding']} < {gates['grounding']}")
    if report["acl_leaks"]:
        failures.append(f"{len(report['acl_leaks'])} ACL leak(s): {report['acl_leaks'][:5]}")
    if report["filter_violations"]:
        failures.append(f"{len(report['filter_violations'])} filter violation(s): {report['filter_violations'][:5]}")

    summary = markdown(report, gates, failures, "live providers" if args.live else "stub embeddings, offline")
    print(summary)
    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(summary + "\n")
    if args.json:
        args.json.write_text(json.dumps({**report, "gates": gates, "failures": failures}, ensure_ascii=False, indent=2), encoding="utf-8")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
