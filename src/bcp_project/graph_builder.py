"""Build document relationship graphs from semantic similarity and shared entities."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .qdrant_store import QdrantIndexer


def _normalize_entity(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _summary_entities(summary: Optional[dict]) -> Dict[str, Set[str]]:
    summary = summary or {}
    entities: Dict[str, Set[str]] = {
        "organization": set(),
        "project": set(),
        "person": set(),
        "keyword": set(),
    }

    core = summary.get("core_info") or {}
    if isinstance(core, dict):
        org = _normalize_entity(str(core.get("organization") or ""))
        if org:
            entities["organization"].add(org)

    for project in summary.get("major_projects") or []:
        if isinstance(project, dict):
            name = _normalize_entity(str(project.get("project_name") or ""))
            if name:
                entities["project"].add(name)

    for person in summary.get("key_personnel") or []:
        if isinstance(person, dict):
            name = _normalize_entity(str(person.get("name") or ""))
            if name:
                entities["person"].add(name)

    for kw in summary.get("searchable_keywords") or []:
        norm = _normalize_entity(str(kw or ""))
        if norm and len(norm) >= 3:
            entities["keyword"].add(norm)

    return entities


def summary_entities(summary: Optional[dict]) -> Dict[str, Set[str]]:
    """Public alias used by API routes when expanding related documents."""
    return _summary_entities(summary)

@dataclass
class GraphDoc:
    doc_id: str
    doc_type: str
    doc_date: str
    summary_json: Optional[dict] = None


@dataclass
class GraphBuildResult:
    nodes: List[dict] = field(default_factory=list)
    edges: List[dict] = field(default_factory=list)


def document_summary_card(doc: GraphDoc) -> dict:
    """Compact document metadata for mind-map document picker."""
    payload = _node_payload(doc)
    return {
        "doc_id": payload["id"],
        "title": payload["title"],
        "doc_type": payload["doc_type"],
        "doc_date": payload["doc_date"],
        "organization": payload.get("organization") or "",
    }


def _node_payload(doc: GraphDoc, *, is_center: bool = False) -> dict:
    summary = doc.summary_json or {}
    core = summary.get("core_info") if isinstance(summary.get("core_info"), dict) else {}
    projects = summary.get("major_projects") or []
    project_names = [
        p.get("project_name")
        for p in projects
        if isinstance(p, dict) and p.get("project_name")
    ][:3]
    meeting_title = (core or {}).get("meeting_title") or ""
    title = meeting_title or (project_names[0] if project_names else doc.doc_type)
    return {
        "id": doc.doc_id,
        "label": doc.doc_id,
        "title": title,
        "doc_type": doc.doc_type,
        "doc_date": doc.doc_date,
        "organization": (core or {}).get("organization") or "",
        "projects": project_names,
        "is_center": is_center,
    }


def _add_edge(
    edges: Dict[Tuple[str, str, str], dict],
    source: str,
    target: str,
    edge_type: str,
    *,
    weight: float,
    label: str = "",
) -> None:
    if source == target:
        return
    a, b = sorted((source, target))
    key = (a, b, edge_type)
    existing = edges.get(key)
    payload = {
        "source": a,
        "target": b,
        "type": edge_type,
        "weight": round(weight, 4),
        "label": label,
    }
    if existing is None or payload["weight"] > existing["weight"]:
        edges[key] = payload


def build_entity_edges(docs: List[GraphDoc]) -> Dict[Tuple[str, str, str], dict]:
    edges: Dict[Tuple[str, str, str], dict] = {}
    index: Dict[Tuple[str, str], Set[str]] = defaultdict(set)

    for doc in docs:
        entities = _summary_entities(doc.summary_json)
        for entity_type, values in entities.items():
            for value in values:
                index[(entity_type, value)].add(doc.doc_id)

    for (entity_type, value), doc_ids in index.items():
        if len(doc_ids) < 2:
            continue
        ids = sorted(doc_ids)
        weight_by_type = {
            "organization": 0.95,
            "project": 0.88,
            "person": 0.82,
            "keyword": 0.72,
        }
        weight = weight_by_type.get(entity_type, 0.7)
        label = value if entity_type != "keyword" else f"Keyword: {value}"
        for i, source in enumerate(ids):
            for target in ids[i + 1 :]:
                _add_edge(edges, source, target, entity_type, weight=weight, label=label)

    return edges


def build_semantic_edges_from_hits(
    center_doc_id: str,
    similar_hits: Iterable[dict],
    *,
    allowed: Optional[Set[str]] = None,
) -> Dict[Tuple[str, str, str], dict]:
    """Build semantic edges from one Qdrant similarity result (no extra remote calls)."""
    edges: Dict[Tuple[str, str, str], dict] = {}
    for row in similar_hits:
        other = row.get("doc_id")
        if not other or other == center_doc_id:
            continue
        if allowed is not None and other not in allowed:
            continue
        score = float(row.get("score") or 0)
        _add_edge(
            edges,
            center_doc_id,
            other,
            "semantic",
            weight=score,
            label=f"Similar ({int(score * 100)}%)",
        )
    return edges


def build_document_graph(
    docs: List[GraphDoc],
    qdrant: Optional[QdrantIndexer] = None,
    *,
    center_doc_id: Optional[str] = None,
    semantic_neighbors: int = 5,
    min_similarity: float = 0.68,
    include_semantic: bool = True,
    include_entities: bool = True,
    semantic_hits: Optional[List[dict]] = None,
    use_provided_cluster: bool = False,
) -> GraphBuildResult:
    """Build a document relationship graph.

    Relation types (from document summaries):
    - semantic: embedding similarity of summaries (Qdrant)
    - project / person / organization / keyword: shared extracted entities

    For large archives, pass a curated related cluster with
    ``use_provided_cluster=True`` so the API can resolve neighbors across
    the full corpus without scanning every row here.
    """
    if not docs:
        return GraphBuildResult()

    doc_map = {d.doc_id: d for d in docs}
    hits: List[dict] = list(semantic_hits or [])

    if (
        include_semantic
        and not hits
        and qdrant is not None
        and center_doc_id
        and center_doc_id in doc_map
    ):
        hits = qdrant.find_similar_documents(
            center_doc_id,
            limit=semantic_neighbors + 8,
            min_score=min_similarity,
        )

    if center_doc_id and center_doc_id in doc_map and not use_provided_cluster:
        related_ids = {center_doc_id}
        for row in hits:
            other = row.get("doc_id")
            if other and other in doc_map:
                related_ids.add(other)
        if include_entities:
            center_entities = _summary_entities(doc_map[center_doc_id].summary_json)
            for doc in docs:
                if doc.doc_id == center_doc_id:
                    continue
                other_entities = _summary_entities(doc.summary_json)
                for entity_type in ("organization", "project", "person", "keyword"):
                    if center_entities[entity_type] & other_entities[entity_type]:
                        related_ids.add(doc.doc_id)
                        break
                if len(related_ids) >= semantic_neighbors + 12:
                    break
        working_docs = [doc_map[did] for did in related_ids if did in doc_map]
    else:
        working_docs = docs

    doc_ids = {d.doc_id for d in working_docs}

    edge_map: Dict[Tuple[str, str, str], dict] = {}
    if include_entities:
        edge_map.update(build_entity_edges(working_docs))
    if include_semantic and center_doc_id and hits:
        edge_map.update(
            build_semantic_edges_from_hits(
                center_doc_id,
                hits,
                allowed=doc_ids,
            )
        )

    nodes = [
        _node_payload(doc, is_center=(doc.doc_id == center_doc_id))
        for doc in working_docs
    ]
    return GraphBuildResult(nodes=nodes, edges=list(edge_map.values()))


def related_documents_payload(
    center: GraphDoc,
    graph: GraphBuildResult,
    *,
    limit: int = 12,
) -> dict:
    """Rank neighbors of center doc for viewer sidebar."""
    scores: Dict[str, float] = defaultdict(float)
    reasons: Dict[str, List[str]] = defaultdict(list)

    for edge in graph.edges:
        if edge["source"] == center.doc_id:
            other = edge["target"]
        elif edge["target"] == center.doc_id:
            other = edge["source"]
        else:
            continue
        scores[other] = max(scores[other], edge["weight"])
        label = edge.get("label") or edge.get("type", "related")
        if label not in reasons[other]:
            reasons[other].append(label)

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
    node_map = {n["id"]: n for n in graph.nodes}

    related = []
    for doc_id, score in ranked:
        node = node_map.get(doc_id, {})
        related.append(
            {
                "doc_id": doc_id,
                "doc_type": node.get("doc_type", "Document"),
                "doc_date": node.get("doc_date", ""),
                "score": round(score, 4),
                "reasons": reasons.get(doc_id, []),
            }
        )

    return {
        "center": center.doc_id,
        "related": related,
        "graph": {
            "nodes": graph.nodes,
            "edges": graph.edges,
        },
    }
