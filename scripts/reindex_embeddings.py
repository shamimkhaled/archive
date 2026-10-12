#!/usr/bin/env python3
"""Re-embed the archive into a new embedding space without a search outage.

Each process searches the collection that matches its own AI_EMBED_* settings
(``document_chunks__<model>__<dim>``), so the switch is a rolling deploy:

1. Build:   python scripts/reindex_embeddings.py --provider local --model bge-m3 --dim 1024
            Copies every point from the current collections, re-embedding the text
            with the target model. Payloads and point ids are preserved, so chunk
            text is not re-OCRed. Restricted documents are skipped when the target
            is a cloud provider and the egress policy forbids it.
2. Deploy:  set AI_EMBED_PROVIDER / AI_EMBED_MODEL / AI_EMBED_DIM on every service.
            Old replicas keep querying the old collection until they are replaced.
3. Catch up: rerun step 1 with --catch-up; it only embeds points that are missing
            from the target (uploads that landed on old replicas during the rollout).
4. Alias:   --point-alias makes the logical names (document_summaries,
            document_chunks) point at the new collections for tools and dashboards.
5. Retire:  --drop-source deletes the previous collections once you are satisfied.
            Rolling back before this step is just restoring the old env.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project import ai  # noqa: E402
from bcp_project.ai.egress import EgressBlocked  # noqa: E402
from bcp_project.ai.providers import EMBED_DIMENSIONS  # noqa: E402
from bcp_project.ai.routing import Route  # noqa: E402
from bcp_project.qdrant_store import (  # noqa: E402
    CHUNKS_COLLECTION,
    LOGICAL_COLLECTIONS,
    SUMMARIES_COLLECTION,
    build_summary_embedding_text,
    make_qdrant_indexer,
    physical_collection_name,
)


def _text_for(logical: str, payload: Dict[str, Any]) -> str:
    if logical == SUMMARIES_COLLECTION:
        return build_summary_embedding_text(payload) or str(payload.get("doc_id") or "")
    return str(payload.get("text") or "")


def _doc_id(payload: Dict[str, Any]) -> str:
    return str(payload.get("doc_id") or payload.get("parent_doc_id") or payload.get("document_id") or "")


async def _sensitivities() -> Dict[str, str]:
    from sqlalchemy import select

    from bcp_project.db import get_session
    from bcp_project.models import DocumentRecord

    async with get_session() as db:
        rows = (await db.execute(select(DocumentRecord.doc_id, DocumentRecord.sensitivity))).all()
    return {doc_id: sensitivity for doc_id, sensitivity in rows}


def _target_ids(indexer, target: str) -> Set[Any]:
    ids: Set[Any] = set()
    offset = None
    while True:
        points, offset = indexer.raw_client.scroll(
            collection_name=target, limit=512, offset=offset, with_payload=False, with_vectors=False
        )
        ids.update(p.id for p in points)
        if offset is None:
            return ids


def copy_collection(
    indexer,
    *,
    logical: str,
    source: str,
    target: str,
    route: Route,
    sensitivities: Dict[str, str],
    batch: int,
    catch_up: bool,
    dry_run: bool,
) -> Dict[str, int]:
    from qdrant_client.http import models as rest

    stats = {"copied": 0, "skipped_existing": 0, "skipped_policy": 0, "empty": 0}
    existing = _target_ids(indexer, target) if catch_up else set()
    offset = None
    while True:
        points, offset = indexer.raw_client.scroll(
            collection_name=source, limit=batch, offset=offset, with_payload=True, with_vectors=False
        )
        pending: List[Any] = []
        for point in points:
            if point.id in existing:
                stats["skipped_existing"] += 1
                continue
            payload = dict(point.payload or {})
            text = _text_for(logical, payload)
            if not text.strip():
                stats["empty"] += 1
                continue
            pending.append((point.id, payload, text))
        if pending and not dry_run:
            by_level: Dict[str, List[Any]] = {}
            for item in pending:
                by_level.setdefault(sensitivities.get(_doc_id(item[1]), "internal"), []).append(item)
            for level, items in by_level.items():
                try:
                    vectors = ai.embed([t for _, _, t in items], sensitivity=level, route=route)
                except EgressBlocked:
                    stats["skipped_policy"] += len(items)
                    continue
                indexer.raw_client.upsert(
                    collection_name=target,
                    points=[rest.PointStruct(id=pid, vector=vec, payload=payload) for (pid, payload, _), vec in zip(items, vectors)],
                    wait=True,
                )
                stats["copied"] += len(items)
        elif pending:
            stats["copied"] += len(pending)
        if offset is None:
            return stats


async def run(args: argparse.Namespace) -> int:
    current = ai.embed_route()
    provider = (args.provider or current.provider).lower()
    model = args.model or current.model
    dim = int(args.dim or EMBED_DIMENSIONS.get(model.lower()) or EMBED_DIMENSIONS.get(model) or current.params["dim"])
    route = Route(task="embed", provider=provider, model=model, params={"dim": dim}, source="reindex")

    indexer = make_qdrant_indexer()
    known = indexer._known_names(refresh=True)
    aliases = indexer.aliases()
    sensitivities = await _sensitivities()
    print(f"Target embedding space: {provider}/{model} ({dim}-d); egress policy {ai.egress_policy()}")

    for logical in LOGICAL_COLLECTIONS:
        target = physical_collection_name(logical, model, dim)
        explicit = args.source_suffix is not None
        source = f"{logical}{args.source_suffix}" if explicit else indexer.resolve_collection(logical)
        if source == logical and logical in aliases:
            source = aliases[logical]
        if args.drop_source:
            legacy = logical in known and logical not in aliases
            victim = source if explicit else (logical if legacy else None)
            if victim is None or victim == target or victim not in known:
                print(f"{logical}: nothing to drop (pass --source-suffix to name the old collection)")
            elif args.dry_run:
                print(f"{logical}: would drop {victim}")
            else:
                indexer.raw_client.delete_collection(victim)
                print(f"{logical}: dropped {victim}")
            continue
        if source == target:
            print(f"{logical}: already on {target}")
        elif source not in known:
            print(f"{logical}: source {source} does not exist; creating empty {target}")
            if not args.dry_run:
                indexer.ensure_physical_collection(target, dim)
        else:
            if not args.dry_run:
                indexer.ensure_physical_collection(target, dim)
            stats = copy_collection(
                indexer,
                logical=logical,
                source=source,
                target=target,
                route=route,
                sensitivities=sensitivities,
                batch=args.batch,
                catch_up=args.catch_up,
                dry_run=args.dry_run,
            )
            print(f"{logical}: {source} -> {target} {stats}")
        if args.point_alias and not args.dry_run:
            if logical in known and logical not in aliases:
                print(
                    f"{logical}: a pre-versioning collection still uses this name; "
                    "run --drop-source first, then --point-alias"
                )
            else:
                indexer.point_alias(logical, target)
                print(f"{logical}: alias -> {target}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", help="Target embedding provider (default: current AI_EMBED_PROVIDER)")
    parser.add_argument("--model", help="Target embedding model (default: current AI_EMBED_MODEL)")
    parser.add_argument("--dim", type=int, help="Target vector dimension (default: known size for the model)")
    parser.add_argument("--batch", type=int, default=64)
    parser.add_argument("--catch-up", action="store_true", help="Only embed points missing from the target")
    parser.add_argument("--point-alias", action="store_true", help="Point the logical names at the target")
    parser.add_argument("--drop-source", action="store_true", help="Delete the source collections")
    parser.add_argument("--source-suffix", help="Explicit source, e.g. __text-embedding-3-small__1536")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
