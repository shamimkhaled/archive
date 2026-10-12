"""TOTP (RFC 6238) step-up for privileged roles.

Secrets are Fernet-encrypted at rest with ``MFA_ENCRYPTION_KEY`` (falls back to a
key derived from ``JWT_SECRET_KEY``). A used time-step is recorded so a code can
never be replayed inside its validity window.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from datetime import datetime
from typing import Iterable, List, Optional, Tuple
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import is_production
from .models import Role, User, UserMfa

TOTP_PERIOD = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1
RECOVERY_CODE_COUNT = 8
ISSUER = "Sonali Bank Archive"


def mfa_required_roles() -> set[str]:
    raw = os.getenv("MFA_REQUIRED_ROLES", "admin,board_secretary")
    return {part.strip() for part in raw.split(",") if part.strip()}


def mfa_enforced() -> bool:
    raw = os.getenv("MFA_ENFORCE")
    if raw is None:
        return is_production()
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def role_requires_mfa(role: Role | str) -> bool:
    value = role.value if isinstance(role, Role) else str(role)
    return mfa_enforced() and value in mfa_required_roles()


def _derived_key(jwt_secret: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(f"mfa:{jwt_secret}".encode()).digest())


def _fernet() -> MultiFernet:
    """Encrypts with the current key; decrypts with current or previous keys during a rotation."""
    from .auth import PREVIOUS_SECRET_KEY, SECRET_KEY

    current = (os.getenv("MFA_ENCRYPTION_KEY") or "").strip()
    previous = (os.getenv("MFA_ENCRYPTION_KEY_PREVIOUS") or "").strip()
    keys = [current.encode() if current else _derived_key(SECRET_KEY)]
    if previous:
        keys.append(previous.encode())
    keys.append(_derived_key(SECRET_KEY))
    if PREVIOUS_SECRET_KEY:
        keys.append(_derived_key(PREVIOUS_SECRET_KEY))
    unique = list(dict.fromkeys(keys))
    return MultiFernet([Fernet(k) for k in unique])


def rewrap_secret(token: str) -> Optional[str]:
    """Re-encrypt under the current key (after a rotation); None if no known key opens it."""
    try:
        return _fernet().rotate(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(token: str) -> Optional[str]:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _b32decode(secret: str) -> bytes:
    cleaned = secret.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def totp_at(secret: str, step: int) -> str:
    digest = hmac.new(_b32decode(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def current_step(now: Optional[float] = None) -> int:
    return int((now if now is not None else time.time()) // TOTP_PERIOD)


def match_step(secret: str, code: str, *, now: Optional[float] = None, last_used_step: Optional[int] = None) -> Optional[int]:
    """Return the matching time-step, or None. Steps at or before ``last_used_step`` are rejected."""
    digits = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(digits) != TOTP_DIGITS:
        return None
    base = current_step(now)
    for delta in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        step = base + delta
        if last_used_step is not None and step <= last_used_step:
            continue
        if hmac.compare_digest(totp_at(secret, step), digits):
            return step
    return None


def provisioning_uri(secret: str, username: str) -> str:
    label = quote(f"{ISSUER}:{username}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}"
    )


def _hash_recovery(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace("-", "").encode()).hexdigest()


def generate_recovery_codes() -> Tuple[List[str], List[str]]:
    plain = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(RECOVERY_CODE_COUNT)]
    return plain, [_hash_recovery(code) for code in plain]


async def get_enrollment(db: AsyncSession, username: str) -> Optional[UserMfa]:
    result = await db.execute(select(UserMfa).where(UserMfa.username == username))
    return result.scalar_one_or_none()


async def start_enrollment(db: AsyncSession, user: User) -> Tuple[UserMfa, str]:
    """Create (or rotate an unconfirmed) enrolment. Returns the row and the plain secret."""
    row = await get_enrollment(db, user.username)
    secret = generate_secret()
    if row is None:
        row = UserMfa(username=user.username, secret_encrypted=encrypt_secret(secret), enabled=False)
        db.add(row)
    elif row.enabled:
        raise ValueError("MFA already enabled")
    else:
        row.secret_encrypted = encrypt_secret(secret)
    await db.commit()
    await db.refresh(row)
    return row, secret


async def confirm_enrollment(db: AsyncSession, row: UserMfa, code: str) -> Optional[List[str]]:
    """Enable MFA when ``code`` is valid. Returns one-time recovery codes."""
    secret = decrypt_secret(row.secret_encrypted)
    if not secret:
        return None
    step = match_step(secret, code)
    if step is None:
        return None
    plain, hashed = generate_recovery_codes()
    row.enabled = True
    row.last_used_step = step
    row.recovery_code_hashes = hashed
    row.confirmed_at = datetime.utcnow()
    await db.commit()
    return plain


async def verify_login_code(db: AsyncSession, row: UserMfa, code: str) -> bool:
    if not row.enabled:
        return False
    secret = decrypt_secret(row.secret_encrypted)
    if secret:
        step = match_step(secret, code, last_used_step=row.last_used_step)
        if step is not None:
            row.last_used_step = step
            await db.commit()
            return True
    return await _consume_recovery_code(db, row, code)


async def _consume_recovery_code(db: AsyncSession, row: UserMfa, code: str) -> bool:
    hashes: Iterable[str] = row.recovery_code_hashes or []
    candidate = _hash_recovery(code or "")
    remaining = [value for value in hashes if not hmac.compare_digest(value, candidate)]
    if len(remaining) == len(list(row.recovery_code_hashes or [])):
        return False
    row.recovery_code_hashes = remaining
    await db.commit()
    return True


async def reset_enrollment(db: AsyncSession, username: str) -> bool:
    row = await get_enrollment(db, username)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True
