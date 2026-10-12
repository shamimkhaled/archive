"""Map a verified OIDC / LDAP identity onto a local user row (provision, role sync, deprovision)."""

from __future__ import annotations

import os
import secrets
from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..auth import get_password_hash
from ..models import Role, User
from ..ports.identity import ExternalIdentity, IdentityError


def _truthy(name: str, default: str = "") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


async def resolve_external_user(
    db: AsyncSession,
    identity: ExternalIdentity,
    *,
    role_map: Dict[str, str],
    auto_provision: bool,
    ip_address: Optional[str] = None,
) -> User:
    """Return the active local user for a verified identity, or raise IdentityError.

    With a role map configured the directory is authoritative: the mapped role is synced onto the
    user every sign-in, and a user in no mapped group is refused (removing someone from the
    archive groups revokes access at their next sign-in).
    """
    user = (await db.execute(select(User).where(User.username == identity.username))).scalar_one_or_none()
    directory_roles = bool(role_map) and _truthy("IDENTITY_SYNC_ROLES", "1")
    if directory_roles and identity.role is None:
        raise IdentityError(f"{identity.source}_role", "no archive role granted by directory groups")

    if user is None:
        if not auto_provision or identity.role is None:
            raise IdentityError(f"{identity.source}_user", "no matching local user")
        user = User(
            username=identity.username,
            email=identity.email,
            hashed_password=get_password_hash(secrets.token_urlsafe(32)),
            role=Role(identity.role),
            is_active=True,
        )
        db.add(user)
        await db.flush()
        await write_audit(
            db,
            username=identity.username,
            action="user_provisioned",
            resource_type="user",
            resource_id=identity.username,
            detail=f"source={identity.source} role={identity.role}",
            ip_address=ip_address,
            commit=True,
        )
        return user

    if not user.is_active:
        raise IdentityError("inactive", "local account disabled")
    if directory_roles and identity.role and user.role.value != identity.role:
        previous = user.role.value
        user.role = Role(identity.role)
        await write_audit(
            db,
            username=identity.username,
            action="user_role_synced",
            resource_type="user",
            resource_id=identity.username,
            detail=f"source={identity.source} {previous}->{identity.role}",
            ip_address=ip_address,
            commit=False,
        )
    if identity.email and not user.email:
        user.email = identity.email
    await db.commit()
    return user
