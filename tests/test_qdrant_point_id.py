import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project.qdrant_store import DOC_POINT_NAMESPACE, normalize_doc_point_id


def test_doc_id_maps_to_deterministic_uuid():
    first = normalize_doc_point_id("SB-2026-001")
    second = normalize_doc_point_id("SB-2026-001")
    assert first == second
    assert isinstance(first, uuid.UUID)
    assert first == uuid.uuid5(DOC_POINT_NAMESPACE, "SB-2026-001")


def test_numeric_and_uuid_ids_are_preserved():
    assert normalize_doc_point_id("42") == 42
    sample = uuid.uuid4()
    assert normalize_doc_point_id(str(sample)) == sample
