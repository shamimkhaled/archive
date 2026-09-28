"""Phase B live caption fan-out (in-process hub + Redis pub/sub)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, Optional, Set

import redis.asyncio as redis

from .cache import REDIS_URL, _note_redis_failure, get_redis_client

logger = logging.getLogger("bcp_project.tx_live")

CHANNEL_PREFIX = "tx:meeting:"

# meeting_id → subscriber queues (same API process)
_hub: Dict[int, Set[asyncio.Queue]] = {}
_hub_lock = asyncio.Lock()


def channel_name(meeting_id: int) -> str:
    return f"{CHANNEL_PREFIX}{int(meeting_id)}"


async def subscribe_local(meeting_id: int) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    async with _hub_lock:
        _hub.setdefault(int(meeting_id), set()).add(queue)
    return queue


async def unsubscribe_local(meeting_id: int, queue: asyncio.Queue) -> None:
    async with _hub_lock:
        holders = _hub.get(int(meeting_id))
        if not holders:
            return
        holders.discard(queue)
        if not holders:
            _hub.pop(int(meeting_id), None)


async def _fanout_local(meeting_id: int, payload: Dict[str, Any]) -> None:
    async with _hub_lock:
        holders = list(_hub.get(int(meeting_id), set()))
    for queue in holders:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass


async def publish_caption(meeting_id: int, event: Dict[str, Any]) -> None:
    """Push a caption event via Redis (preferred) or local hub fallback."""
    payload = dict(event)
    payload.setdefault("meeting_id", int(meeting_id))
    try:
        client = get_redis_client()
        receivers = await client.publish(channel_name(meeting_id), json.dumps(payload))
        if receivers:
            return
        # No Redis subscribers yet — still fan out locally (same-process STT).
        await _fanout_local(meeting_id, payload)
    except Exception as exc:
        _note_redis_failure("tx_publish", exc)
        await _fanout_local(meeting_id, payload)


async def iter_captions(meeting_id: int) -> AsyncIterator[Dict[str, Any]]:
    """Yield caption events for a meeting (local hub + Redis when available)."""
    mid = int(meeting_id)
    local_q = await subscribe_local(mid)
    redis_task: Optional[asyncio.Task] = None
    stop = asyncio.Event()

    async def _redis_pump() -> None:
        client = None
        pubsub = None
        try:
            client = redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=None,
                retry_on_timeout=False,
            )
            pubsub = client.pubsub()
            await pubsub.subscribe(channel_name(mid))
            while not stop.is_set():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    await asyncio.sleep(0.05)
                    continue
                data = message.get("data")
                if not data:
                    continue
                try:
                    payload = json.loads(data)
                except (TypeError, json.JSONDecodeError):
                    continue
                await _fanout_local(mid, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _note_redis_failure("tx_subscribe", exc)
            while not stop.is_set():
                await asyncio.sleep(2.0)
        finally:
            try:
                if pubsub is not None:
                    await pubsub.unsubscribe(channel_name(mid))
                    await pubsub.aclose()
            except Exception:
                pass
            try:
                if client is not None:
                    await client.aclose()
            except Exception:
                pass

    try:
        redis_task = asyncio.create_task(_redis_pump())
        while True:
            try:
                item = await asyncio.wait_for(local_q.get(), timeout=25.0)
                yield item
            except asyncio.TimeoutError:
                yield {"type": "ping", "meeting_id": mid}
    finally:
        stop.set()
        if redis_task is not None:
            redis_task.cancel()
            try:
                await redis_task
            except (asyncio.CancelledError, Exception):
                pass
        await unsubscribe_local(mid, local_q)
