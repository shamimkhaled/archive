import os
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
PROJECT_ROOT_STR = str(PROJECT_ROOT)

_PLACEHOLDER_MARKERS = ("your_", "changeme", "example", "xxx")


def load_environment() -> None:
    """Load environment variables from the project .env file when present."""
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=False)
    else:
        load_dotenv(override=False)


def _is_placeholder(value: Optional[str]) -> bool:
    if value is None:
        return True
    stripped = value.strip()
    if not stripped:
        return True
    lower = stripped.lower()
    return any(marker in lower for marker in _PLACEHOLDER_MARKERS)


def app_env() -> str:
    return os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development")).strip().lower()


def is_production() -> bool:
    return app_env() in {"production", "prod"}


def require_secret(name: str, *, min_length: int = 32) -> str:
    value = os.getenv(name)
    if _is_placeholder(value) or (value is not None and len(value.strip()) < min_length and is_production()):
        raise RuntimeError(
            f"{name} must be set to a strong non-placeholder value "
            f"(min {min_length} chars) in production."
        )
    assert value is not None
    return value.strip()


def normalize_database_url(url: Optional[str] = None) -> str:
    """Convert Neon/psycopg URLs to SQLAlchemy asyncpg form."""
    raw = (url or os.getenv("DATABASE_URL") or "postgresql+asyncpg://postgres:postgres@localhost:5432/bcpdb").strip()
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    if raw.startswith("postgresql://"):
        raw = "postgresql+asyncpg://" + raw[len("postgresql://") :]

    parsed = urlparse(raw)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    # asyncpg uses `ssl=require`; Neon often ships `sslmode` / `channel_binding`.
    if "sslmode" in query:
        query.setdefault("ssl", query.pop("sslmode"))
    query.pop("channel_binding", None)
    if "ssl" not in query and "neon.tech" in (parsed.hostname or ""):
        query["ssl"] = "require"

    return urlunparse(parsed._replace(query=urlencode(query)))


def resolve_redis_url() -> str:
    """
    Prefer REDIS_URL (redis:// or rediss://).

    If REDIS_URL is missing/placeholder/localhost and Upstash REST vars are set,
    build a TLS Redis URL that redis-py can use:
      rediss://default:<UPSTASH_REDIS_REST_TOKEN>@<host-from-REST-URL>:6379
    (Upstash REST URL alone is not enough for redis-py.)
    """
    from urllib.parse import quote, urlparse

    raw = (os.getenv("REDIS_URL") or "").strip().strip('"')

    def _is_local_or_empty(url: str) -> bool:
        if not url or _is_placeholder(url):
            return True
        lower = url.lower()
        return "127.0.0.1" in lower or "localhost" in lower

    if raw and not _is_local_or_empty(raw):
        return raw

    rest_url = (os.getenv("UPSTASH_REDIS_REST_URL") or "").strip().strip('"')
    rest_token = (os.getenv("UPSTASH_REDIS_REST_TOKEN") or "").strip().strip('"')
    if rest_url and rest_token and not _is_placeholder(rest_token):
        host = urlparse(rest_url).hostname
        if host:
            return f"rediss://default:{quote(rest_token, safe='')}@{host}:6379"

    if raw:
        return raw
    return "redis://127.0.0.1:6379/0"


def resolve_qdrant_settings() -> Tuple[str, Optional[str]]:
    """
    Return (url, api_key).
    Supports:
      - QDRANT_URL / QDRANT_API_KEY
      - QDRANT_CLUSTER_ENDPOINT / QDRANT_CLUSTER_API (aliases)
      - QDRANT_HOST + QDRANT_PORT (local docker)
    """
    api_key = (
        os.getenv("QDRANT_API_KEY")
        or os.getenv("QDRANT_CLUSTER_API")
        or os.getenv("QDRANT_CLUSTER_API_KEY")
    )
    if api_key:
        api_key = api_key.strip().strip('"')

    url = os.getenv("QDRANT_URL") or os.getenv("QDRANT_CLUSTER_ENDPOINT")
    if url:
        return url.strip().rstrip("/").strip('"'), api_key or None

    host = os.getenv("QDRANT_HOST", "127.0.0.1").strip()
    port = int(os.getenv("QDRANT_PORT", "6333"))
    scheme = "https" if api_key else "http"
    return f"{scheme}://{host}:{port}", api_key or None


def resolve_openai_credentials() -> Tuple[str, Optional[str]]:
    """
    Return (api_key, base_url).
    Uses OPENAI_API_KEY when real; otherwise OPENROUTER_API_KEY with OpenRouter base URL.
    """
    openai_key = os.getenv("OPENAI_API_KEY")
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")

    if openai_key and not _is_placeholder(openai_key):
        return openai_key.strip(), (base_url.strip() if base_url else None)

    if openrouter_key and not _is_placeholder(openrouter_key):
        return openrouter_key.strip(), (base_url.strip() if base_url else "https://openrouter.ai/api/v1")

    raise RuntimeError(
        "Set a real OPENAI_API_KEY or OPENROUTER_API_KEY for embeddings and summarization."
    )


def openai_chat_model() -> str:
    explicit = os.getenv("OPENAI_CHAT_MODEL")
    if explicit:
        return explicit.strip()
    _, base_url = resolve_openai_credentials()
    if base_url and "openrouter.ai" in base_url:
        return "openai/gpt-4.1-mini"
    return "gpt-4.1-mini"


def openai_embedding_model() -> str:
    explicit = os.getenv("OPENAI_EMBEDDING_MODEL")
    if explicit:
        return explicit.strip()
    try:
        _, base_url = resolve_openai_credentials()
    except RuntimeError:
        return "text-embedding-3-small"
    if base_url and "openrouter.ai" in base_url:
        return "openai/text-embedding-3-small"
    return "text-embedding-3-small"


def resolve_resend_api_key() -> Optional[str]:
    key = os.getenv("RESEND_API_KEY") or os.getenv("RESEND_EMAIL_API")
    if key and not _is_placeholder(key):
        return key.strip()
    return None


def cookie_secure() -> bool:
    explicit = os.getenv("COOKIE_SECURE")
    if explicit is not None:
        return explicit.strip().lower() in {"1", "true", "yes", "on"}
    return is_production()
