import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project.access_control import (
    can_download_with_grant,
    can_view_with_grant,
    is_archive_privileged,
)
from bcp_project.models import AccessMode, AccessRequestStatus, DocumentAccessRequest, Role, User
from bcp_project.qdrant_store import (
    build_summary_embedding_text,
    reciprocal_rank_fusion,
    _casefold_contains,
    _contains_bangla,
)
from bcp_project.security import LoginRateLimiter
from datetime import datetime, timedelta
from types import SimpleNamespace


def _user(role: Role, username: str = "u1") -> User:
    return User(
        id=1,
        username=username,
        hashed_password="x",
        role=role,
        is_active=True,
    )


def test_privileged_roles_bypass():
    assert is_archive_privileged(_user(Role.admin))
    assert is_archive_privileged(_user(Role.board_secretary))
    assert is_archive_privileged(_user(Role.board_member))
    assert not is_archive_privileged(_user(Role.uploader))


def test_board_member_has_direct_view_access():
    member = _user(Role.board_member)
    assert can_view_with_grant(member, None)

    grant = DocumentAccessRequest(
        doc_id="D1",
        requester_username="u1",
        purpose="review",
        requested_mode=AccessMode.view_only,
        status=AccessRequestStatus.approved,
        expires_at=datetime.utcnow() + timedelta(days=1),
    )
    assert can_view_with_grant(member, grant)
    assert not can_download_with_grant(member, grant)

    grant.requested_mode = AccessMode.download
    assert can_view_with_grant(member, grant)
    assert not can_download_with_grant(member, grant)


def test_expired_grant_denies():
    member = _user(Role.uploader)
    grant = DocumentAccessRequest(
        doc_id="D1",
        requester_username="u1",
        purpose="review",
        requested_mode=AccessMode.download,
        status=AccessRequestStatus.approved,
        expires_at=datetime.utcnow() - timedelta(hours=1),
    )
    assert not can_view_with_grant(member, grant)
    assert not can_download_with_grant(member, grant)


def test_admin_can_always_view_download():
    admin = _user(Role.admin)
    assert can_view_with_grant(admin, None)
    assert can_download_with_grant(admin, None)


def test_secretary_views_but_cannot_download():
    secretary = _user(Role.board_secretary)
    assert can_view_with_grant(secretary, None)
    assert not can_download_with_grant(secretary, None)


def test_member_cannot_download_even_with_grant():
    member = _user(Role.board_member)
    grant = DocumentAccessRequest(
        doc_id="D1",
        requester_username="u1",
        purpose="review",
        requested_mode=AccessMode.download,
        status=AccessRequestStatus.approved,
        expires_at=datetime.utcnow() + timedelta(days=1),
    )
    assert can_view_with_grant(member, grant)
    assert not can_download_with_grant(member, grant)


def test_embedding_text_includes_structured_fields():
    text = build_summary_embedding_text(
        {
            "searchable_keywords": ["Board", "বোর্ড"],
            "core_info": {"organization": "Sonali Bank PLC"},
            "key_personnel": [{"name": "Director", "role": "Chair"}],
            "major_projects": [{"project_name": "Stadium", "brief_context": "Dhaka"}],
            "finance_and_admin": ["Budget FY26"],
        }
    )
    assert "Board" in text
    assert "বোর্ড" in text
    assert "Sonali Bank PLC" in text
    assert "Stadium" in text
    assert "Budget FY26" in text


def test_casefold_contains_unicode():
    assert _casefold_contains("বোর্ড সভা", "বোর্ড")
    assert _casefold_contains("Board Meeting", "board")


def test_reciprocal_rank_fusion_prefers_shared_top_hits():
    fused = reciprocal_rank_fusion(
        [
            ["A", "B", "C"],
            ["B", "A", "D"],
        ]
    )
    assert fused["A"] > fused["C"]
    assert fused["B"] > fused["C"]
    assert fused["B"] >= fused["A"]


def test_contains_bangla_detects_script():
    assert _contains_bangla("Board and বোর্ড")
    assert not _contains_bangla("Board only")


def test_login_rate_limiter_blocks_after_max():
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=600)
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"))
    for _ in range(3):
        assert not limiter.is_blocked(request, "alice")
        limiter.record_failure(request, "alice")
    assert limiter.is_blocked(request, "alice")
    limiter.clear(request, "alice")
    assert not limiter.is_blocked(request, "alice")
