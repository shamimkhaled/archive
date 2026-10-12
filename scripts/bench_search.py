#!/usr/bin/env python3
"""Measure end-to-end archive search latency against the configured Postgres, Qdrant, and AI providers.

Runs the same ``hybrid_search`` the web UI and /api/v1 use, with the cache off, for the
queries in ``eval/retrieval_bilingual.jsonl`` (or ``--query``), and reports p50/p95/max.
Acceptance target: p95 under 1.5 s.

Example:
  PYTHONPATH=src python scripts/bench_search.py --rounds 2
  PYTHONPATH=src python scripts/bench_search.py --role board_member --query "ICT procurement"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bcp_project.config import load_environment

load_environment()

from bcp_project.db import get_session
from bcp_project.search_acl import PRIVILEGED_ROLES, AclScope
from bcp_project.services.search import hybrid_search


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--query", action="append", default=[], help="Query to run (repeatable)")
    parser.add_argument("--queries", type=Path, default=ROOT / "eval" / "retrieval_bilingual.jsonl")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--role", default="board_member")
    parser.add_argument("--target-ms", type=float, default=1500.0)
    args = parser.parse_args()

    queries = args.query or [
        json.loads(line)["q"] for line in args.queries.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    acl = AclScope(unrestricted=args.role in PRIVILEGED_ROLES, role=args.role)
    timings = []
    modes = {}
    async with get_session() as db:
        await hybrid_search(db, acl=acl, q="warm up", use_cache=False)
        for _ in range(args.rounds):
            for q in queries:
                started = time.perf_counter()
                outcome = await hybrid_search(db, acl=acl, q=q, use_cache=False)
                timings.append((time.perf_counter() - started) * 1000)
                modes[outcome.mode] = modes.get(outcome.mode, 0) + 1

    ordered = sorted(timings)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    print(
        f"searches={len(timings)} modes={modes} p50={statistics.median(timings):.0f}ms "
        f"p95={p95:.0f}ms max={ordered[-1]:.0f}ms target<{args.target_ms:.0f}ms"
    )
    if p95 >= args.target_ms:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
