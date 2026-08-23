import json
import logging
import os
from typing import Any, Optional
from urllib.parse import urlparse

import redis.asyncio as redis

from .config import load_environment, resolve_redis_url

load_environment()

logger = logging.getLogger("bcp_project.cache")

REDIS_URL = resolve_redis_url()
SEARCH_CACHE_TTL_SECONDS = int(os.getenv("SEARCH_CACHE_TTL_SECONDS", "3600"))
SEARCH_CACHE_VERSION_KEY = "search:version"

_redis_client: Optional["redis.Redis"] = None
_redis_unavailable_logged = False


def _redis_host_for_log(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.hostname or "unknown"
    except Exception:
        return "unknown"


def get_redis_client() -> "redis.Redis":
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            retry_on_timeout=False,
            health_check_interval=30,
        )
        logger.info("Redis search cache targeting host=%s", _redis_host_for_log(REDIS_URL))
    return _redis_client


def _note_redis_failure(action: str, exc: Exception) -> None:
    """Log the first Redis outage at WARNING without a traceback storm."""
    global _redis_unavailable_logged
    if not _redis_unavailable_logged:
        logger.warning(
            "Redis %s failed (%s: %s); continuing without cache. host=%s",
            action,
            type(exc).__name__,
            exc,
            _redis_host_for_log(REDIS_URL),
        )
        _redis_unavailable_logged = True
    else:
        logger.debug("Redis %s failed again: %s", action, exc)


def _search_cache_key(query: str, version: str, *, lang: Optional[str] = None) -> str:
    normalized = query.strip().casefold()
    lang_part = (lang or "any").strip().lower()
    return f"search:init:v{version}:{lang_part}:{normalized}"


def _metadata_cache_key(filters: dict, version: str) -> str:
    serialized = json.dumps(filters, sort_keys=True, ensure_ascii=False)
    return f"search:meta:v{version}:{serialized}"


async def get_cached_search(query: str, lang: Optional[str] = None) -> Optional[Any]:
    """Best-effort cache read. Any Redis failure is treated as a cache miss
    so search keeps working (just uncached) when Redis is unavailable."""
    try:
        client = get_redis_client()
        version = await client.get(SEARCH_CACHE_VERSION_KEY) or "0"
        raw = await client.get(_search_cache_key(query, version, lang=lang))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        _note_redis_failure("read", exc)
        return None


async def set_cached_search(
    query: str, payload: Any, ttl: int = SEARCH_CACHE_TTL_SECONDS, lang: Optional[str] = None
) -> None:
    try:
        client = get_redis_client()
        version = await client.get(SEARCH_CACHE_VERSION_KEY) or "0"
        await client.set(_search_cache_key(query, version, lang=lang), json.dumps(payload), ex=ttl)
    except Exception as exc:
        _note_redis_failure("write", exc)


async def get_cached_metadata_search(filters: dict) -> Optional[Any]:
    try:
        client = get_redis_client()
        version = await client.get(SEARCH_CACHE_VERSION_KEY) or "0"
        raw = await client.get(_metadata_cache_key(filters, version))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        _note_redis_failure("meta read", exc)
        return None


async def set_cached_metadata_search(filters: dict, payload: Any, ttl: int = SEARCH_CACHE_TTL_SECONDS) -> None:
    try:
        client = get_redis_client()
        version = await client.get(SEARCH_CACHE_VERSION_KEY) or "0"
        await client.set(_metadata_cache_key(filters, version), json.dumps(payload), ex=ttl)
    except Exception as exc:
        _note_redis_failure("meta write", exc)


async def bump_search_cache_version() -> None:
    """Invalidate all cached search results, e.g. after a document is uploaded."""
    try:
        client = get_redis_client()
        await client.incr(SEARCH_CACHE_VERSION_KEY)
    except Exception as exc:
        _note_redis_failure("version bump", exc)
