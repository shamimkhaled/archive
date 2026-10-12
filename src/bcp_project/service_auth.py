"""Service client API-key authentication and Redis rate limiting."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from typing import Dict, FrozenSet, Optional, Tuple

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import get_password_hash, verify_password
from .models import ServiceClient, User
from .policy import Principal, Scope, principal_from_user, scopes_for_role

logger = logging.getLogger("bcp_project.service_auth")

API_KEY_HEADER = "X-API-Key"
ON_BEHALF_HEADER = "X-On-Behalf-Of"
DEFAULT_RATE_LIMIT = int(os.getenv("API_V1_RATE_LIMIT_PER_MINUTE", "120"))


def generate_api_key() -> Tuple[str, str, str]:
    """Return (raw_key, key_prefix, key_hash). Raw key shown once at creation."""
    raw = f"bcp_{secrets.token_urlsafe(32)}"
    prefix = raw[:12]
    return raw, prefix, get_password_hash(raw)


_VERIFIED_KEYS: Dict[str, Tuple[int, str, float]] = {}
VERIFIED_KEY_TTL_SECONDS = float(os.getenv("API_KEY_CACHE_SECONDS") or 300)


def _key_fingerprint(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


async def lookup_service_client(db: AsyncSession, raw_key: str) -> Optional[ServiceClient]:
    """bcrypt-verify the key once, then trust its SHA-256 for a few minutes.

    The client row is re-read on every call, so deactivating a client or rotating its key
    takes effect immediately; only the slow hash check is skipped.
    """
    if not raw_key or not raw_key.startswith("bcp_"):
        return None
    prefix = raw_key[:12]
    statement = select(ServiceClient).where(
        ServiceClient.key_prefix == prefix,
        ServiceClient.is_active.is_(True),
    )
    clients = (await db.execute(statement)).scalars().all()
    fingerprint = _key_fingerprint(raw_key)
    cached = _VERIFIED_KEYS.get(fingerprint)
    if cached and cached[2] > time.monotonic():
        for client in clients:
            if client.id == cached[0] and client.key_hash == cached[1]:
                return client
    for client in clients:
        try:
            if verify_password(raw_key, client.key_hash):
                if len(_VERIFIED_KEYS) > 10_000:
                    _VERIFIED_KEYS.clear()
                _VERIFIED_KEYS[fingerprint] = (client.id, client.key_hash, time.monotonic() + VERIFIED_KEY_TTL_SECONDS)
                return client
        except Exception:
            continue
    _VERIFIED_KEYS.pop(fingerprint, None)
    return None


def scopes_from_client(client: ServiceClient) -> FrozenSet[str]:
    return frozenset(str(s) for s in (client.scopes or []) if s)


def merge_service_scopes(
    client_scopes: FrozenSet[str],
    user: Optional[User],
    *,
    is_agent: bool = False,
) -> FrozenSet[str]:
    """Effective scopes for a service call.

    Acting on behalf of a user narrows the client to that user's role, including
    privileged view: an agent never sees more than the human it represents.
    Agents without a bound user never get privileged view.
    """
    if user is None:
        if is_agent:
            return frozenset(client_scopes - {Scope.docs_search_privileged.value})
        return client_scopes
    user_scopes = scopes_for_role(user.role)
    allowed = set()
    for scope in client_scopes:
        if scope == Scope.mcp_invoke.value or scope in user_scopes:
            allowed.add(scope)
    return frozenset(allowed)


def agent_requires_on_behalf() -> bool:
    return (os.getenv("AGENT_REQUIRE_ON_BEHALF_OF", "1").strip().lower()) in {"1", "true", "yes", "on"}


async def resolve_on_behalf_user(db: AsyncSession, username: Optional[str]) -> Optional[User]:
    if not username:
        return None
    statement = select(User).where(User.username == username.strip())
    result = await db.execute(statement)
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="X-On-Behalf-Of user not found or inactive",
        )
    return user


async def check_rate_limit(bucket_key: str, limit_per_minute: int) -> None:
    """Per-minute limit for /api/v1 callers; falls back to per-replica counters without Redis."""
    from .rate_limit import hit

    await hit("api_v1", bucket_key, limit_per_minute)


async def authenticate_api_v1_principal(request: Request, db: AsyncSession) -> Principal:
    """Authenticate via X-API-Key (service/agent) or JWT Bearer/cookie (user)."""
    from .deps import get_current_user

    api_key = (request.headers.get(API_KEY_HEADER) or "").strip()
    if api_key:
        client = await lookup_service_client(db, api_key)
        if client is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
        limit = int(client.rate_limit_per_minute or DEFAULT_RATE_LIMIT)
        await check_rate_limit(f"svc:{client.client_id}", limit)

        on_behalf = (request.headers.get(ON_BEHALF_HEADER) or "").strip() or None
        if client.is_agent and not on_behalf and agent_requires_on_behalf():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Agent clients must send {ON_BEHALF_HEADER}",
            )
        user = await resolve_on_behalf_user(db, on_behalf)
        scopes = merge_service_scopes(scopes_from_client(client), user, is_agent=bool(client.is_agent))
        return Principal(
            actor_id=client.client_id,
            actor_type="agent" if client.is_agent else "service",
            scopes=scopes,
            role=user.role if user else None,
            user=user,
            client_name=client.name,
        )

    user = await get_current_user(request, db)
    await check_rate_limit(f"user:{user.username}", DEFAULT_RATE_LIMIT)
    return principal_from_user(user)
