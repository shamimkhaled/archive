import os
from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from .config import load_environment, normalize_database_url

load_environment()

DATABASE_URL = normalize_database_url()

# (pool_size, max_overflow) per Railway service. Sum across replicas must stay under the
# Postgres (or Neon pooler) connection limit; override with DB_POOL_SIZE / DB_MAX_OVERFLOW.
POOL_DEFAULTS = {"web": (10, 10), "worker": (4, 4), "pipeline": (3, 2)}


def pool_settings(url: str = DATABASE_URL, role: str = "") -> Dict[str, Any]:
    if not url.startswith("postgresql"):
        return {}
    role = (role or os.getenv("SERVICE_ROLE") or "web").strip().lower()
    size, overflow = POOL_DEFAULTS.get(role, POOL_DEFAULTS["web"])
    return {
        "pool_size": int(os.getenv("DB_POOL_SIZE") or size),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW") or overflow),
        "pool_timeout": float(os.getenv("DB_POOL_TIMEOUT") or 10),
        "pool_recycle": int(os.getenv("DB_POOL_RECYCLE") or 1800),
    }


engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    **pool_settings(),
)
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


def get_session() -> AsyncSession:
    return AsyncSessionLocal()
