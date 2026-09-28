"""Distributed locks via Redis SET NX EX."""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from ..cache import _note_redis_failure, get_redis_client

logger = logging.getLogger("bcp_project.jobs.locks")


class RedisLock:
    def __init__(self, key: str, *, ttl_seconds: int = 60, token: Optional[str] = None) -> None:
        self.key = key
        self.ttl_seconds = ttl_seconds
        self.token = token or uuid.uuid4().hex
        self.acquired = False

    async def acquire(self) -> bool:
        try:
            client = get_redis_client()
            ok = await client.set(self.key, self.token, nx=True, ex=self.ttl_seconds)
            self.acquired = bool(ok)
            return self.acquired
        except Exception as exc:
            _note_redis_failure("lock_acquire", exc)
            return False

    async def release(self) -> None:
        if not self.acquired:
            return
        try:
            client = get_redis_client()
            # Release only if we still own the lock.
            script = """
            if redis.call('get', KEYS[1]) == ARGV[1] then
                return redis.call('del', KEYS[1])
            else
                return 0
            end
            """
            await client.eval(script, 1, self.key, self.token)
        except Exception as exc:
            _note_redis_failure("lock_release", exc)
        finally:
            self.acquired = False


@asynccontextmanager
async def redis_lock(key: str, *, ttl_seconds: int = 60) -> AsyncIterator[bool]:
    """Yield True if the lock was acquired."""
    prefix = os.getenv("JOB_LOCK_PREFIX", "bcp:lock:")
    lock = RedisLock(f"{prefix}{key}", ttl_seconds=ttl_seconds)
    acquired = await lock.acquire()
    try:
        yield acquired
    finally:
        await lock.release()
