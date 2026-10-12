import os
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from .config import is_production, load_environment, require_secret

load_environment()

if is_production():
    SECRET_KEY = require_secret("JWT_SECRET_KEY", min_length=32)
else:
    SECRET_KEY = os.getenv("JWT_SECRET_KEY", "supersecretlocalkey")
# During a rotation, tokens signed with the previous key stay valid until they expire.
PREVIOUS_SECRET_KEY = (os.getenv("JWT_SECRET_KEY_PREVIOUS") or "").strip() or None
ALGORITHM = "HS256"
# Default 8 hours; override with ACCESS_TOKEN_EXPIRE_MINUTES in .env
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


MFA_PENDING_MINUTES = 5


def create_access_token(subject: str, role: str, *, mfa: bool = False) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": subject, "role": role, "exp": expire}
    if mfa:
        payload["mfa"] = True
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_mfa_pending_token(subject: str) -> str:
    """Short-lived proof that the password step passed; never a session."""
    expire = datetime.utcnow() + timedelta(minutes=MFA_PENDING_MINUTES)
    payload = {"sub": subject, "purpose": "mfa_pending", "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_oidc_flow_token(state: str, nonce: str, verifier: str) -> str:
    """Signed state/nonce/PKCE verifier for one OIDC round-trip; never a session."""
    expire = datetime.utcnow() + timedelta(minutes=10)
    payload = {"purpose": "oidc_flow", "state": state, "nonce": nonce, "verifier": verifier, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_oidc_flow_token(token: str) -> Optional[dict]:
    payload = decode_access_token(token)
    if not payload or payload.get("purpose") != "oidc_flow":
        return None
    return payload


def decode_mfa_pending_token(token: str) -> Optional[str]:
    payload = decode_access_token(token)
    if not payload or payload.get("purpose") != "mfa_pending":
        return None
    return payload.get("sub")


def decode_access_token(token: str) -> Optional[dict]:
    for key in (SECRET_KEY, PREVIOUS_SECRET_KEY):
        if not key:
            continue
        try:
            return jwt.decode(token, key, algorithms=[ALGORITHM])
        except JWTError:
            continue
    return None


def decode_session_token(token: str) -> Optional[dict]:
    """Access token only — rejects MFA-pending and other purpose-bound tokens."""
    payload = decode_access_token(token)
    if not payload or payload.get("purpose"):
        return None
    return payload


def token_seconds_remaining(payload: dict) -> Optional[float]:
    """Return seconds until JWT expiry, or None if missing/invalid."""
    exp = payload.get("exp")
    if exp is None:
        return None
    try:
        return float(exp) - datetime.utcnow().timestamp()
    except (TypeError, ValueError):
        return None


def should_refresh_access_token(payload: dict) -> bool:
    """Refresh when less than half the session lifetime remains."""
    remaining = token_seconds_remaining(payload)
    if remaining is None or remaining <= 0:
        return False
    lifetime = ACCESS_TOKEN_EXPIRE_MINUTES * 60
    return remaining < (lifetime * 0.5)
