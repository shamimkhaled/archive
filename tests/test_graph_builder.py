import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project.graph_builder import GraphDoc, build_document_graph, related_documents_payload


def _docs():
    return [
        GraphDoc(
            doc_id="SB-2026-001",
            doc_type="Minutes",
            doc_date="2026-01-10",
            summary_json={
                "core_info": {"organization": "Sonali Bank PLC"},
                "major_projects": [{"project_name": "Digital Core", "brief_context": "Core banking"}],
                "key_personnel": [{"name": "A. Rahman", "role": "Director"}],
                "searchable_keywords": ["board", "budget"],
            },
        ),
        GraphDoc(
            doc_id="SB-2026-002",
            doc_type="Memo",
            doc_date="2026-01-12",
            summary_json={
                "core_info": {"organization": "Sonali Bank PLC"},
                "major_projects": [{"project_name": "Digital Core", "brief_context": "Phase 2"}],
                "key_personnel": [{"name": "A. Rahman", "role": "Director"}],
                "searchable_keywords": ["technology"],
            },
        ),
        GraphDoc(
            doc_id="SB-2026-003",
            doc_type="Report",
            doc_date="2026-02-01",
            summary_json={
                "core_info": {"organization": "Other Org"},
                "major_projects": [{"project_name": "Branch Network", "brief_context": ""}],
                "searchable_keywords": ["audit"],
            },
        ),
    ]


def test_entity_edges_connect_shared_project_and_org():
    graph = build_document_graph(
        _docs(),
        qdrant=None,
        include_semantic=False,
        include_entities=True,
    )
    edge_pairs = {(e["source"], e["target"], e["type"]) for e in graph.edges}
    assert ("SB-2026-001", "SB-2026-002", "organization") in edge_pairs
    assert ("SB-2026-001", "SB-2026-002", "project") in edge_pairs
    assert ("SB-2026-001", "SB-2026-002", "person") in edge_pairs


def test_center_graph_limits_to_related_cluster():
    graph = build_document_graph(
        _docs(),
        qdrant=None,
        center_doc_id="SB-2026-001",
        include_semantic=False,
        include_entities=True,
    )
    node_ids = {n["id"] for n in graph.nodes}
    assert "SB-2026-001" in node_ids
    assert "SB-2026-002" in node_ids
    assert "SB-2026-003" not in node_ids


def test_related_documents_payload_ranks_neighbors():
    docs = _docs()
    graph = build_document_graph(docs, qdrant=None, center_doc_id="SB-2026-001", include_semantic=False)
    center = docs[0]
    payload = related_documents_payload(center, graph, limit=5)
    assert payload["center"] == "SB-2026-001"
    assert payload["related"]
    assert payload["related"][0]["doc_id"] == "SB-2026-002"
