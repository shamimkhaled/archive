"""Phase 1: permission-aware results, service scopes, sensitivity, and TOTP MFA."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from bcp_project.access_control import (
    enrich_results_with_access,
    hidden_restricted_doc_ids,
    redact_unviewable,
    viewable_doc_ids,
)
from bcp_project.auth import (
    create_access_token,
    create_mfa_pending_token,
    decode_mfa_pending_token,
    decode_session_token,
)
from bcp_project.mfa import (
    confirm_enrollment,
    match_step,
    start_enrollment,
    totp_at,
    verify_login_code,
)
from bcp_project.models import (
    AccessMode,
    AccessRequestStatus,
    DocumentAccessRequest,
    DocumentRecord,
    Role,
    Sensitivity,
    User,
    parse_sensitivity,
)
from bcp_project.policy import Scope
from bcp_project.service_auth import merge_service_scopes


def _user(role: Role, username: str = "member1") -> User:
    return User(id=1, username=username, hashed_password="x", role=role, is_active=True)


def _doc(doc_id: str, sensitivity: str = "internal") -> DocumentRecord:
    return DocumentRecord(
        doc_id=doc_id,
        doc_date=date(2026, 1, 5),
        doc_type="Board Memo",
        keywords=["secret-term"],
        summary_json={"core_info": {"meeting_title": "Confidential title"}},
        file_location="/tmp/x.pdf",
        uploaded_by="uploader1",
        sensitivity=sensitivity,
    )


def _hit(doc_id: str) -> dict:
    return {
        "doc_id": doc_id,
        "doc_type": "Board Memo",
        "doc_date": "2026-01-05",
        "snippet": "loan write-off for customer X",
        "searchable_keywords": ["secret-term"],
        "uploaded_by": "uploader1",
        "match_reasons": ["chunk"],
        "relevance_label": "Strong match",
    }


async def _grant(db, username: str, doc_id: str) -> None:
    db.add(
        DocumentAccessRequest(
            doc_id=doc_id,
            requester_username=username,
            purpose="review",
            requested_mode=AccessMode.view_only,
            status=AccessRequestStatus.approved,
            expires_at=datetime.utcnow() + timedelta(days=1),
        )
    )
    await db.commit()


def test_redact_keeps_only_discovery_fields():
    row = redact_unviewable({**_hit("D1"), "can_view": False, "access_status": "none"})
    assert set(row) <= {"doc_id", "doc_type", "doc_date", "relevance_label", "can_view", "access_status"}
    assert "snippet" not in row and "searchable_keywords" not in row and "uploaded_by" not in row


@pytest.mark.asyncio
async def test_member_without_grant_sees_no_excerpt(db_session):
    db_session.add_all([_doc("D1"), _doc("D2")])
    await db_session.commit()
    member = _user(Role.board_member)
    await _grant(db_session, member.username, "D2")

    rows = {r["doc_id"]: r for r in await enrich_results_with_access(db_session, member, [_hit("D1"), _hit("D2")])}
    assert rows["D1"]["can_view"] is False
    assert "snippet" not in rows["D1"]
    assert "searchable_keywords" not in rows["D1"]
    assert rows["D2"]["can_view"] is True
    assert rows["D2"]["snippet"]


@pytest.mark.asyncio
async def test_restricted_documents_are_invisible_without_grant(db_session):
    db_session.add_all([_doc("R1", "restricted"), _doc("R2", "restricted"), _doc("I1")])
    await db_session.commit()
    member = _user(Role.board_member)
    await _grant(db_session, member.username, "R2")

    rows = await enrich_results_with_access(db_session, member, [_hit("R1"), _hit("R2"), _hit("I1")])
    ids = [r["doc_id"] for r in rows]
    assert "R1" not in ids
    assert "R2" in ids and "I1" in ids
    assert await hidden_restricted_doc_ids(db_session, member, ["R1", "R2", "I1"]) == {"R1"}


@pytest.mark.asyncio
async def test_privileged_roles_see_everything(db_session):
    db_session.add(_doc("R1", "restricted"))
    await db_session.commit()
    secretary = _user(Role.board_secretary, "sec")
    rows = await enrich_results_with_access(db_session, secretary, [_hit("R1")])
    assert rows[0]["can_view"] is True and rows[0]["snippet"]
    assert await viewable_doc_ids(db_session, secretary, ["R1"]) == {"R1"}


def test_agent_never_exceeds_on_behalf_user():
    client = frozenset({Scope.docs_search.value, Scope.docs_search_privileged.value, Scope.mcp_invoke.value})
    merged = merge_service_scopes(client, _user(Role.board_member), is_agent=True)
    assert Scope.docs_search_privileged.value not in merged
    admin_merged = merge_service_scopes(client, _user(Role.admin, "adm"), is_agent=True)
    assert Scope.docs_search_privileged.value in admin_merged
    unbound_agent = merge_service_scopes(client, None, is_agent=True)
    assert Scope.docs_search_privileged.value not in unbound_agent
    unbound_service = merge_service_scopes(client, None, is_agent=False)
    assert Scope.docs_search_privileged.value in unbound_service


def test_parse_sensitivity_defaults_to_internal():
    assert parse_sensitivity("RESTRICTED") == Sensitivity.restricted.value
    assert parse_sensitivity("bogus") == Sensitivity.internal.value
    assert parse_sensitivity(None) == Sensitivity.internal.value


def test_totp_matches_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32("12345678901234567890")
    assert totp_at(secret, 59 // 30) == "287082"
    assert totp_at(secret, 1111111109 // 30) == "081804"


def test_totp_rejects_replayed_step():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    now = 1111111109.0
    step = match_step(secret, "081804", now=now)
    assert step == 1111111109 // 30
    assert match_step(secret, "081804", now=now, last_used_step=step) is None


@pytest.mark.asyncio
async def test_mfa_enrolment_and_login(db_session, monkeypatch):
    user = _user(Role.admin, "admin1")
    row, secret = await start_enrollment(db_session, user)
    import bcp_project.mfa as mfa

    assert row.enabled is False
    code = totp_at(secret, mfa.current_step())
    recovery = await confirm_enrollment(db_session, row, code)
    assert recovery and len(recovery) == mfa.RECOVERY_CODE_COUNT
    assert row.enabled is True

    assert not await verify_login_code(db_session, row, code)  # same step cannot be replayed
    assert await verify_login_code(db_session, row, recovery[0])
    assert not await verify_login_code(db_session, row, recovery[0])  # recovery codes are single-use


def test_mfa_session_and_pending_tokens_are_distinct(monkeypatch):
    monkeypatch.setenv("MFA_ENFORCE", "1")
    from bcp_project.deps import session_payload

    pending = create_mfa_pending_token("admin1")
    assert decode_mfa_pending_token(pending) == "admin1"
    assert decode_session_token(pending) is None

    no_mfa = create_access_token("admin1", Role.admin.value)
    assert session_payload(no_mfa) is None
    with_mfa = create_access_token("admin1", Role.admin.value, mfa=True)
    assert session_payload(with_mfa)["sub"] == "admin1"
    member = create_access_token("m1", Role.board_member.value)
    assert session_payload(member)["sub"] == "m1"
