"""Per-user and per-service-key rate limits in Redis, with an in-process fallback.

Fixed one-minute windows keyed by bucket and identity. When Redis is unreachable each replica
keeps its own counters instead of failing open, so a Redis outage cannot remove the limits.
Limits come from ``RATE_LIMIT_<BUCKET>_PER_MINUTE``; 0 disables a bucket.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Dict, Optional, Tuple

from fastapi import Depends, HTTPException, status

logger = logging.getLogger("bcp_project.rate_limit")

DEFAULT_LIMITS: Dict[str, int] = {
    "search": 60,
    "ask": 20,
    "upload": 30,
    "ai_job": 10,
    "recording": 6,
    "api_v1": 120,
}
WINDOW_SECONDS = 60

_local: Dict[Tuple[str, int], int] = {}
_local_lock = threading.Lock()


def limit_for(bucket: str) -> int:
    raw = os.getenv(f"RATE_LIMIT_{bucket.upper()}_PER_MINUTE")
    if raw is not None and raw.strip():
        return int(raw)
    return DEFAULT_LIMITS.get(bucket, 60)


def _window(now: float) -> Tuple[int, int]:
    window = int(now // WINDOW_SECONDS)
    retry_after = WINDOW_SECONDS - int(now % WINDOW_SECONDS)
    return window, max(1, retry_after)


def _local_hit(key: str, window: int) -> int:
    with _local_lock:
        if len(_local) > 50_000:
            for stale in [k for k in _local if k[1] < window]:
                _local.pop(stale, None)
        count = _local.get((key, window), 0) + 1
        _local[(key, window)] = count
        return count


def reset_local_counters_for_tests() -> None:
    with _local_lock:
        _local.clear()


async def hit(bucket: str, identity: str, limit: Optional[int] = None, *, now: Optional[float] = None) -> None:
    """Count one request; raise 429 with Retry-After when over the limit."""
    from .cache import _note_redis_failure, get_redis_client

    limit = limit_for(bucket) if limit is None else int(limit)
    if limit <= 0:
        return
    window, retry_after = _window(now if now is not None else time.time())
    key = f"bcp:rl:{bucket}:{identity}"
    try:
        client = get_redis_client()
        redis_key = f"{key}:{window}"
        pipe = client.pipeline(transaction=False)
        pipe.incr(redis_key)
        pipe.expire(redis_key, WINDOW_SECONDS * 2)
        count = int((await pipe.execute())[0])
    except Exception as exc:
        _note_redis_failure("rate_limit", exc)
        count = _local_hit(key, window)
    if count > limit:
        from .observability import incr

        incr("rate_limit_rejected", labels={"bucket": bucket})
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please wait a moment and try again.",
            headers={"Retry-After": str(retry_after)},
        )


def rate_limit(bucket: str) -> Callable:
    """Route dependency: limits the signed-in user per bucket."""
    from .deps import get_current_user

    async def dependency(current_user=Depends(get_current_user)) -> None:
        await hit(bucket, f"user:{current_user.username}")

    dependency.__name__ = f"rate_limit_{bucket}"
    return dependency
