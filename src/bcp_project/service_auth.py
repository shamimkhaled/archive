"""Service client API-key authentication and Redis rate limiting."""

from __future__ import annotations

import logging
import os
import secrets
from typing import FrozenSet, Optional, Tuple

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import get_password_hash, verify_password
from .cache import _note_redis_failure, get_redis_client
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


async def lookup_service_client(db: AsyncSession, raw_key: str) -> Optional[ServiceClient]:
    if not raw_key or not raw_key.startswith("bcp_"):
        return None
    prefix = raw_key[:12]
    statement = select(ServiceClient).where(
        ServiceClient.key_prefix == prefix,
        ServiceClient.is_active.is_(True),
    )
    result = await db.execute(statement)
    for client in result.scalars().all():
        try:
            if verify_password(raw_key, client.key_hash):
                return client
        except Exception:
            continue
    return None


def scopes_from_client(client: ServiceClient) -> FrozenSet[str]:
    return frozenset(str(s) for s in (client.scopes or []) if s)


def merge_service_scopes(client_scopes: FrozenSet[str], user: Optional[User]) -> FrozenSet[str]:
    """Intersect client scopes with user role scopes; privileged search stays client-side."""
    if user is None:
        return client_scopes
    user_scopes = scopes_for_role(user.role)
    allowed = set()
    for scope in client_scopes:
        if scope == Scope.docs_search_privileged.value:
            allowed.add(scope)
        elif scope == Scope.mcp_invoke.value:
            allowed.add(scope)
        elif scope in user_scopes:
            allowed.add(scope)
    return frozenset(allowed)


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
    """Reject when over limit; fail-open if Redis is unavailable."""
    if limit_per_minute <= 0:
        return
    try:
        client = get_redis_client()
        key = f"bcp:ratelimit:{bucket_key}"
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, 60)
        if int(count) > limit_per_minute:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
            )
    except HTTPException:
        raise
    except Exception as exc:
        _note_redis_failure("rate_limit", exc)


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
        user = await resolve_on_behalf_user(db, on_behalf)
        scopes = merge_service_scopes(scopes_from_client(client), user)
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
