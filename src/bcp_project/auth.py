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
ALGORITHM = "HS256"
# Default 8 hours; override with ACCESS_TOKEN_EXPIRE_MINUTES in .env
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(subject: str, role: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": subject, "role": role, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


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
