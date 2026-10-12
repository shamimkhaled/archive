"""Phase 8: audit hash chain, SIEM forwarding, rate limits, pools, batching, PDF ranges, OIDC, LDAP, autoscaling."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select


@pytest_asyncio.fixture
async def maker(tmp_path):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from bcp_project.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'p8.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _request(headers=None):
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw, "client": ("10.0.0.7", 1234)})


async def _add_audit(db, n, *, start=0, age_seconds=60):
    from bcp_project.models import AuditLog

    created = datetime.utcnow() - timedelta(seconds=age_seconds)
    for i in range(start, start + n):
        db.add(AuditLog(username=f"u{i}", action="view_file", resource_type="document", resource_id=f"D{i}", created_at=created))
    await db.commit()


# --- audit hash chain -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seal_and_verify_chain(maker):
    from bcp_project.models import AuditLog
    from bcp_project.services.audit_chain import GENESIS_HASH, row_digest, seal_pending, verify_chain

    async with maker() as db:
        await _add_audit(db, 5)
        assert await seal_pending(db) == 5
        await _add_audit(db, 3, start=5)
        assert await seal_pending(db) == 3
        rows = (await db.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
        assert rows[0].prev_hash == GENESIS_HASH
        for prev, row in zip(rows, rows[1:]):
            assert row.prev_hash == prev.row_hash
            assert row.row_hash == row_digest(prev.row_hash, row)
        result = await verify_chain(db)
    assert result["ok"] and result["linked"] == 8 and result["sealed"] == 8


@pytest.mark.asyncio
async def test_recent_rows_wait_for_seal_delay(maker, monkeypatch):
    from bcp_project.services.audit_chain import seal_pending

    monkeypatch.setenv("AUDIT_SEAL_DELAY_SECONDS", "30")
    async with maker() as db:
        await _add_audit(db, 2, age_seconds=60)
        await _add_audit(db, 2, start=2, age_seconds=0)
        assert await seal_pending(db) == 2


@pytest.mark.asyncio
async def test_verify_detects_edit_middle_delete_and_tail_delete(maker):
    from sqlalchemy import delete, update

    from bcp_project.models import AuditLog
    from bcp_project.services.audit_chain import seal_pending, verify_chain

    async with maker() as db:
        await _add_audit(db, 6)
        await seal_pending(db)

        await db.execute(update(AuditLog).where(AuditLog.id == 3).values(detail="edited"))
        await db.commit()
        edited = await verify_chain(db)
        assert edited["ok"] is False and edited["broken_at"] == 3 and "changed" in edited["reason"]
        await db.execute(update(AuditLog).where(AuditLog.id == 3).values(detail=None))
        await db.commit()
        assert (await verify_chain(db))["ok"]

        await db.execute(delete(AuditLog).where(AuditLog.id == 4))
        await db.commit()
        removed = await verify_chain(db)
        assert removed["ok"] is False and removed["broken_at"] == 5

    async with maker() as db:
        await db.execute(delete(AuditLog))
        await db.commit()
    async with maker() as db:
        from bcp_project.models import IntegrationCursor

        await db.execute(delete(IntegrationCursor))
        await db.commit()
        await _add_audit(db, 4)
        await seal_pending(db)
        await db.execute(delete(AuditLog).where(AuditLog.id == (await db.scalar(select(AuditLog.id).order_by(AuditLog.id.desc()).limit(1)))))
        await db.commit()
        tail = await verify_chain(db)
    assert tail["ok"] is False and "newest" in tail["reason"]


# --- SIEM --------------------------------------------------------------------------------------


def test_cef_escapes_and_carries_hashes():
    from bcp_project.models import AuditLog
    from bcp_project.services.siem import severity, syslog_frame, to_cef

    row = AuditLog(
        id=7, username="sec|x", action="access_denied", resource_type="document", resource_id="D=1",
        detail="line1\nline2", created_at=datetime(2026, 10, 1, 9, 30), row_hash="a" * 64, prev_hash="b" * 64,
    )
    cef = to_cef(row)
    assert cef.startswith("CEF:0|Sonali Bank PLC|Board Archive|1.0|access_denied|access_denied|7|")
    assert "cs2=D\\=1" in cef and "msg=line1\\nline2" in cef
    assert f"cs4={'a' * 64}" in cef and f"cs5={'b' * 64}" in cef
    assert severity("audit_chain_broken") == 7 and severity("login_local_fallback") == 5 and severity("login") == 3
    assert syslog_frame(cef, row, hostname="h").startswith("<108>1 2026-10-01T09:30:00")


class _Sink:
    def __init__(self, fail=False):
        self.batches, self.fail = [], fail

    def send(self, lines):
        if self.fail:
            raise ConnectionError("siem down")
        self.batches.append(list(lines))


@pytest.mark.asyncio
async def test_siem_forwards_sealed_prefix_and_keeps_cursor_on_failure(maker, monkeypatch):
    from bcp_project.models import IntegrationCursor
    from bcp_project.services.audit_chain import seal_pending
    from bcp_project.services.siem import SIEM_CURSOR, forward_audit

    monkeypatch.setenv("SIEM_FORMAT", "json")
    async with maker() as db:
        await _add_audit(db, 3)
        await seal_pending(db)
        await _add_audit(db, 2, start=3, age_seconds=0)

        with pytest.raises(ConnectionError):
            await forward_audit(db, sender=_Sink(fail=True))
        await db.rollback()
        cursor = await db.get(IntegrationCursor, SIEM_CURSOR)
        assert cursor is None or cursor.last_id == 0

        sink = _Sink()
        result = await forward_audit(db, sender=sink)
        assert result == {"sent": 3, "last_id": 3}
        lines = [json.loads(line.split(" - audit - ", 1)[1]) for line in sink.batches[0]]
        assert [row["id"] for row in lines] == [1, 2, 3] and all(row["row_hash"] for row in lines)
        assert (await forward_audit(db, sender=sink))["sent"] == 0


# --- rate limits, API-key cache, pools, batching ---------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_falls_back_locally_and_returns_429(monkeypatch):
    import bcp_project.cache as cache
    from bcp_project.rate_limit import hit, reset_local_counters_for_tests

    def broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr(cache, "get_redis_client", broken)
    reset_local_counters_for_tests()
    now = 1_000_000 * 60 + 5
    for _ in range(3):
        await hit("search", "user:a", 3, now=now)
    with pytest.raises(HTTPException) as exc:
        await hit("search", "user:a", 3, now=now)
    assert exc.value.status_code == 429 and int(exc.value.headers["Retry-After"]) == 55
    await hit("search", "user:b", 3, now=now)
    await hit("search", "user:a", 3, now=now + 60)
    monkeypatch.setenv("RATE_LIMIT_SEARCH_PER_MINUTE", "0")
    for _ in range(10):
        await hit("search", "user:a", now=now)


@pytest.mark.asyncio
async def test_api_key_cache_skips_bcrypt_but_honours_deactivation(maker, monkeypatch):
    import bcp_project.service_auth as sa
    from bcp_project.models import ServiceClient

    raw, prefix, key_hash = sa.generate_api_key()
    async with maker() as db:
        db.add(ServiceClient(client_id="c1", name="c1", key_prefix=prefix, key_hash=key_hash, scopes=[]))
        await db.commit()
        calls = []
        real = sa.verify_password
        monkeypatch.setattr(sa, "verify_password", lambda *a: calls.append(1) or real(*a))
        sa._VERIFIED_KEYS.clear()
        assert (await sa.lookup_service_client(db, raw)).client_id == "c1"
        assert (await sa.lookup_service_client(db, raw)).client_id == "c1"
        assert len(calls) == 1
        client = (await db.execute(select(ServiceClient))).scalar_one()
        client.is_active = False
        await db.commit()
        assert await sa.lookup_service_client(db, raw) is None


def test_pool_settings_per_role(monkeypatch):
    from bcp_project.db import pool_settings

    monkeypatch.delenv("DB_POOL_SIZE", raising=False)
    monkeypatch.delenv("DB_MAX_OVERFLOW", raising=False)
    assert pool_settings("sqlite+aiosqlite:///x.db") == {}
    assert pool_settings("postgresql+asyncpg://h/db", "worker")["pool_size"] == 4
    monkeypatch.setenv("DB_POOL_SIZE", "25")
    web = pool_settings("postgresql+asyncpg://h/db", "web")
    assert web["pool_size"] == 25 and web["max_overflow"] == 10


def test_embed_batches_requests(monkeypatch):
    import bcp_project.ai as ai

    sizes = []

    class Provider:
        def embed(self, *, model, inputs):
            sizes.append(len(inputs))
            return [[float(len(t))] for t in inputs]

    monkeypatch.setattr(ai, "embedding_provider", lambda name: Provider())
    monkeypatch.setattr(ai.egress, "allows", lambda provider, sensitivity: True)
    monkeypatch.setenv("EMBED_BATCH_SIZE", "4")
    route = SimpleNamespace(provider="test", model="m", task="embed", source="test")
    vectors = ai.embed([f"t{i}" for i in range(10)], route=route)
    assert sizes == [4, 4, 2] and len(vectors) == 10


# --- PDF delivery --------------------------------------------------------------------------------


def test_parse_range_variants():
    from bcp_project.pdf_delivery import parse_range

    assert parse_range(None, 100) is None
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=50-500", 100) == (50, 99)
    assert parse_range("bytes=0-1,5-6", 100) is None
    with pytest.raises(ValueError):
        parse_range("bytes=100-", 100)


@pytest.mark.asyncio
async def test_pdf_response_ranges_and_full_stream():
    from bcp_project.pdf_delivery import CHUNK_BYTES, is_initial_request, pdf_response

    data = bytes(range(256)) * 2048
    partial = pdf_response(_request({"Range": "bytes=10-19"}), data, filename="x.pdf")
    assert partial.status_code == 206 and partial.body == data[10:20]
    assert partial.headers["content-range"] == f"bytes 10-19/{len(data)}"
    assert partial.headers["cache-control"] == "no-store"

    bad = pdf_response(_request({"Range": f"bytes={len(data)}-"}), data, filename="x.pdf")
    assert bad.status_code == 416 and bad.headers["content-range"] == f"bytes */{len(data)}"

    full = pdf_response(_request(), data, filename="x.pdf", disposition="attachment")
    assert full.status_code == 200 and full.headers["content-length"] == str(len(data))
    assert full.headers["content-disposition"] == 'attachment; filename="x.pdf"'
    chunks = [chunk async for chunk in full.body_iterator]
    assert b"".join(chunks) == data and max(len(c) for c in chunks) == CHUNK_BYTES

    assert is_initial_request(_request()) and is_initial_request(_request({"Range": "bytes=0-99"}))
    assert not is_initial_request(_request({"Range": "bytes=100-199"}))


@pytest.mark.asyncio
async def test_stamped_pdf_cached_per_user(monkeypatch):
    import bcp_project.pdf_delivery as pd

    stamps = []
    monkeypatch.setattr(pd, "stamp_pdf_bytes", lambda data, seal: stamps.append(seal) or data + seal.encode())
    pd.stamp_cache().clear()

    async def load():
        return b"%PDF"

    a1 = await pd.stamped_pdf(("view", "D1", "alice"), load, "alice")
    a2 = await pd.stamped_pdf(("view", "D1", "alice"), load, "alice")
    b1 = await pd.stamped_pdf(("view", "D1", "bob"), load, "bob")
    assert a1 == a2 == b"%PDFalice" and b1 == b"%PDFbob" and stamps == ["alice", "bob"]


def test_stamp_cache_respects_budget_and_ttl():
    from bcp_project.pdf_delivery import StampCache

    cache = StampCache(max_bytes=10, ttl_seconds=60)
    cache.put("a", b"12345")
    cache.put("b", b"12345")
    cache.put("c", b"12345")
    assert cache.get("a") is None and cache.get("c") == b"12345"
    cache.put("huge", b"x" * 11)
    assert cache.get("huge") is None
    expired = StampCache(max_bytes=10, ttl_seconds=-1)
    expired.put("a", b"1")
    assert expired.get("a") is None


# --- OIDC ---------------------------------------------------------------------------------------


@pytest.fixture
def oidc(monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwk

    from bcp_project.ports.identity import OidcIdentityAdapter

    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("OIDC_CLIENT_ID", "archive")
    monkeypatch.setenv("OIDC_ROLE_MAP", json.dumps({"Archive-Admins": "admin", "Board-Members": "board_member"}))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    public = jwk.construct(public_pem, "RS256").to_dict()
    public.update(kid="k1", use="sig")
    adapter = OidcIdentityAdapter()
    adapter._metadata = {"issuer": "https://idp.example", "authorization_endpoint": "https://idp.example/auth",
                         "token_endpoint": "https://idp.example/token", "jwks_uri": "https://idp.example/jwks"}
    adapter._jwks = {"keys": [public]}
    adapter._jwks_fetched = time.monotonic()
    return adapter, private_pem


def _id_token(private_pem, **overrides):
    from jose import jwt

    now = int(time.time())
    claims = {"iss": "https://idp.example", "aud": "archive", "sub": "s-1", "iat": now, "exp": now + 300,
              "nonce": "n-1", "preferred_username": "rahim", "groups": ["Board-Members"], "amr": ["pwd", "mfa"]}
    claims.update(overrides)
    alg = claims.pop("_alg", "RS256")
    return jwt.encode(claims, private_pem, algorithm=alg, headers={"kid": "k1"})


def test_oidc_accepts_valid_token_and_maps_role(oidc):
    adapter, pem = oidc
    claims = adapter.verify_id_token(_id_token(pem), nonce="n-1")
    identity = adapter.identity_from_claims(claims)
    assert identity.username == "rahim" and identity.role == "board_member" and identity.mfa is True
    url = adapter.authorization_url(state="s", redirect_uri="https://app/cb", nonce="n-1", code_challenge="c")
    assert "code_challenge_method=S256" in url and "nonce=n-1" in url


@pytest.mark.parametrize(
    "overrides,nonce",
    [
        ({}, "other-nonce"),
        ({"aud": "someone-else"}, "n-1"),
        ({"iss": "https://evil.example"}, "n-1"),
        ({"exp": int(time.time()) - 3600}, "n-1"),
    ],
)
def test_oidc_rejects_bad_tokens(oidc, overrides, nonce):
    from bcp_project.ports.identity import IdentityError

    adapter, pem = oidc
    with pytest.raises(IdentityError):
        adapter.verify_id_token(_id_token(pem, **overrides), nonce=nonce)


def test_oidc_rejects_symmetric_and_foreign_keys(oidc):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwt

    from bcp_project.ports.identity import IdentityError

    adapter, _ = oidc
    hs = jwt.encode({"iss": "https://idp.example", "aud": "archive", "nonce": "n-1", "exp": int(time.time()) + 60},
                    "secret", algorithm="HS256", headers={"kid": "k1"})
    with pytest.raises(IdentityError):
        adapter.verify_id_token(hs, nonce="n-1")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with pytest.raises(IdentityError):
        adapter.verify_id_token(_id_token(other), nonce="n-1")


def test_role_map_prefers_most_privileged():
    from bcp_project.ports.identity import map_role

    role_map = {"board-members": "board_member", "archive-admins": "admin", "uploaders": "uploader"}
    assert map_role(["Uploaders", "Archive-Admins"], role_map) == "admin"
    assert map_role(["Board-Members"], role_map) == "board_member"
    assert map_role(["Other"], role_map) is None


@pytest.mark.asyncio
async def test_oidc_callback_ignores_username_query_and_requires_flow_cookie(maker, monkeypatch):
    from bcp_project.routers import auth as auth_router

    monkeypatch.setenv("OIDC_ENABLED", "1")
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/auth/oidc/callback", "headers": [],
             "query_string": b"code=x&state=s&username=admin", "client": ("10.0.0.7", 1)}
    async with maker() as db:
        response = await auth_router.oidc_callback(Request(scope), code="x", state="s", db=db)
    assert response.status_code == 303 and response.headers["location"] == "/login?error=oidc_state"


@pytest.mark.asyncio
async def test_external_user_provision_sync_and_deny(maker):
    from bcp_project.models import Role, User
    from bcp_project.ports.identity import ExternalIdentity, IdentityError
    from bcp_project.services.external_identity import resolve_external_user

    role_map = {"g": "board_member"}
    async with maker() as db:
        with pytest.raises(IdentityError) as exc:
            await resolve_external_user(db, ExternalIdentity("karim", "oidc", role="board_member"), role_map=role_map, auto_provision=False)
        assert exc.value.code == "oidc_user"
        user = await resolve_external_user(db, ExternalIdentity("karim", "oidc", role="board_member"), role_map=role_map, auto_provision=True)
        assert user.role == Role.board_member
        user = await resolve_external_user(db, ExternalIdentity("karim", "oidc", role="admin"), role_map=role_map, auto_provision=False)
        assert user.role == Role.admin
        with pytest.raises(IdentityError) as exc:
            await resolve_external_user(db, ExternalIdentity("karim", "ldap", role=None), role_map=role_map, auto_provision=False)
        assert exc.value.code == "ldap_role"
        stored = (await db.execute(select(User).where(User.username == "karim"))).scalar_one()
        assert stored.role == Role.admin


# --- LDAP ---------------------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, directory, user, password):
        self.directory, self.user, self.password = directory, user, password
        self.entries = []
        self.filters = []

    def bind(self):
        return self.directory["passwords"].get(self.user) == self.password

    def search(self, base, search_filter, attributes, size_limit):
        self.directory["filters"].append(search_filter)
        self.entries = [e for e in self.directory["entries"] if f"={e.entry_attributes_as_dict['sAMAccountName'][0]})" in search_filter]

    def unbind(self):
        return True


def _ldap(monkeypatch):
    from bcp_project.ports.identity import LdapIdentityAdapter

    monkeypatch.setenv("LDAP_URL", "ldaps://dc.example")
    monkeypatch.setenv("LDAP_BASE_DN", "DC=bank")
    monkeypatch.setenv("LDAP_BIND_DN", "CN=svc")
    monkeypatch.setenv("LDAP_BIND_PASSWORD", "svcpw")
    monkeypatch.setenv("LDAP_ROLE_MAP", json.dumps({"CN=Archive-Admins,DC=bank": "admin"}))
    entry = SimpleNamespace(
        entry_dn="CN=Rahim,DC=bank",
        entry_attributes_as_dict={"sAMAccountName": ["rahim"], "mail": ["rahim@bank"], "memberOf": ["CN=Archive-Admins,DC=bank"]},
    )
    directory = {"passwords": {"CN=svc": "svcpw", "CN=Rahim,DC=bank": "right"}, "entries": [entry], "filters": []}
    adapter = LdapIdentityAdapter()
    monkeypatch.setattr(adapter, "_connect", lambda user, password: _FakeConn(directory, user, password))
    return adapter, directory


def test_ldap_bind_search_and_role(monkeypatch):
    from bcp_project.ports.identity import IdentityError

    adapter, directory = _ldap(monkeypatch)
    identity = adapter.authenticate("rahim", "right")
    assert identity.username == "rahim" and identity.role == "admin" and identity.email == "rahim@bank"
    with pytest.raises(IdentityError) as exc:
        adapter.authenticate("rahim", "wrong")
    assert exc.value.code == "invalid"
    with pytest.raises(IdentityError):
        adapter.authenticate("*)(uid=*", "x")
    assert directory["filters"][-1] == "(sAMAccountName=\\2a\\29\\28uid=\\2a)"


@pytest.mark.asyncio
async def test_login_uses_ldap_and_break_glass_allowlist(maker, monkeypatch):
    import bcp_project.ports.identity as identity_port
    from bcp_project.auth import get_password_hash
    from bcp_project.models import Role, User
    from bcp_project.ports.identity import IdentityError
    from bcp_project.routers import auth as auth_router

    adapter, _ = _ldap(monkeypatch)
    monkeypatch.setenv("LDAP_ENABLED", "1")
    monkeypatch.setattr(auth_router, "get_ldap_adapter", lambda: adapter)
    monkeypatch.setenv("LDAP_AUTO_PROVISION", "1")
    async with maker() as db:
        db.add(User(username="breakglass", hashed_password=get_password_hash("local-pw"), role=Role.admin))
        db.add(User(username="stale", hashed_password=get_password_hash("old-pw"), role=Role.admin))
        await db.commit()
        user = await auth_router._authenticate_password(_request(), db, "rahim", "right")
        assert user.username == "rahim" and user.role == Role.admin

        monkeypatch.setenv("LDAP_LOCAL_FALLBACK_USERS", "breakglass")
        assert (await auth_router._authenticate_password(_request(), db, "breakglass", "local-pw")).username == "breakglass"
        with pytest.raises(IdentityError) as exc:
            await auth_router._authenticate_password(_request(), db, "stale", "old-pw")
        assert exc.value.code == "invalid"
    assert identity_port.ldap_local_fallback_allowed("BreakGlass")


# --- secrets ------------------------------------------------------------------------------------


def test_jwt_previous_key_still_decodes(monkeypatch):
    from jose import jwt

    import bcp_project.auth as auth

    old = jwt.encode({"sub": "a", "role": "admin", "exp": datetime.utcnow() + timedelta(minutes=5)}, "old-key-0123456789", algorithm="HS256")
    monkeypatch.setattr(auth, "PREVIOUS_SECRET_KEY", None)
    assert auth.decode_access_token(old) is None
    monkeypatch.setattr(auth, "PREVIOUS_SECRET_KEY", "old-key-0123456789")
    assert auth.decode_access_token(old)["sub"] == "a"


def test_mfa_secret_survives_key_rotation(monkeypatch):
    from cryptography.fernet import Fernet

    import bcp_project.mfa as mfa

    old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv("MFA_ENCRYPTION_KEY", old_key)
    token = mfa.encrypt_secret("JBSWY3DPEHPK3PXP")
    monkeypatch.setenv("MFA_ENCRYPTION_KEY", new_key)
    assert mfa.decrypt_secret(token) is None
    monkeypatch.setenv("MFA_ENCRYPTION_KEY_PREVIOUS", old_key)
    assert mfa.decrypt_secret(token) == "JBSWY3DPEHPK3PXP"
    rewrapped = mfa.rewrap_secret(token)
    monkeypatch.delenv("MFA_ENCRYPTION_KEY_PREVIOUS")
    assert mfa.decrypt_secret(rewrapped) == "JBSWY3DPEHPK3PXP"


# --- autoscaling --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autoscale_up_immediately_down_after_quiet_streak():
    from bcp_project.jobs.autoscale import Pool, autoscale_once, backlog

    assert backlog({"lag": 30, "length": 999, "pending": 5}) == 35
    assert backlog({"lag": None, "length": 12, "pending": 0}) == 12

    calls = []
    railway = SimpleNamespace(replicas=lambda sid: 1, set_replicas=lambda sid, n: calls.append(n))
    pool = Pool("ingest", "s", "svc", min_replicas=1, max_replicas=4, jobs_per_replica=10, scale_down_after=2)
    state: dict = {}

    async def run(jobs):
        stats = {"s": {"lag": jobs, "pending": 0, "length": jobs}}
        return (await autoscale_once(stats=stats, pools=[pool], railway=railway, state=state, apply=True))[0]

    assert (await run(100))["target"] == 4 and calls == [4]
    assert (await run(0))["target"] == 4
    assert (await run(0))["target"] == 1 and calls == [4, 1]
    dry = await autoscale_once(stats={"s": {"lag": 100, "pending": 0}}, pools=[pool], railway=railway, state={}, apply=False)
    assert dry[0]["target"] == 4 and dry[0]["applied"] is False and calls == [4, 1]
