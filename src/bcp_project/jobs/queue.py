"""Redis Streams job transport (ingest / meeting / notify / default).

Each queue is a stream read by one consumer group. A worker that dies mid-job
leaves the entry pending; another worker reclaims it with XAUTOCLAIM after
``JOB_CLAIM_IDLE_MS``. Entries that keep failing move to the dead-letter stream.
The ``enqueue_job`` signature is unchanged from the list-based queue.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import redis.asyncio as redis
from redis.exceptions import ResponseError

from ..cache import _note_redis_failure
from ..config import resolve_redis_url

logger = logging.getLogger("bcp_project.jobs.queue")

QUEUE_KEY = os.getenv("JOB_STREAM_DEFAULT", "bcp:stream:default")
QUEUE_INGEST = os.getenv("JOB_STREAM_INGEST", "bcp:stream:ingest")
QUEUE_MEETING = os.getenv("JOB_STREAM_MEETING", "bcp:stream:meeting")
QUEUE_NOTIFY = os.getenv("JOB_STREAM_NOTIFY", "bcp:stream:notify")
DEAD_LETTER_KEY = os.getenv("JOB_STREAM_DEAD", "bcp:stream:dead")
CONSUMER_GROUP = os.getenv("JOB_CONSUMER_GROUP", "bcp-workers")

# Pre-streams list keys; drained into streams once at worker start.
LEGACY_LIST_KEYS = {
    "bcp:jobs": QUEUE_KEY,
    "bcp:jobs:ingest": QUEUE_INGEST,
    "bcp:jobs:meeting": QUEUE_MEETING,
    "bcp:jobs:notify": QUEUE_NOTIFY,
}

STREAM_MAXLEN = int(os.getenv("JOB_STREAM_MAXLEN", "100000"))
CLAIM_IDLE_MS = int(os.getenv("JOB_CLAIM_IDLE_MS", "300000"))

INGEST_TYPES = frozenset({"chunk_index", "connector_pull", "document_ingest", "board_briefing"})
MEETING_TYPES = frozenset({"meeting_transcribe", "meeting_chunk_stt", "meeting_minutes", "meeting_recording_ingest"})
NOTIFY_TYPES = frozenset({"webhook_dispatch", "audit_export", "siem_forward"})

ALL_STREAMS = (QUEUE_MEETING, QUEUE_INGEST, QUEUE_NOTIFY, QUEUE_KEY)

_stream_client: Optional["redis.Redis"] = None
_groups_ready: set[str] = set()


def job_queue_enabled() -> bool:
    return (os.getenv("ENABLE_JOB_QUEUE") or "").strip().lower() in {"1", "true", "yes", "on"}


def multi_queue_enabled() -> bool:
    return (os.getenv("ENABLE_MULTI_QUEUE") or "1").strip().lower() in {"1", "true", "yes", "on"}


def consumer_name() -> str:
    return os.getenv("JOB_CONSUMER_NAME") or f"{socket.gethostname()}-{os.getpid()}"


def get_stream_client() -> "redis.Redis":
    """Separate client: blocking reads outlive the cache client's 2s socket timeout."""
    global _stream_client
    if _stream_client is None:
        _stream_client = redis.from_url(
            resolve_redis_url(),
            decode_responses=True,
            socket_connect_timeout=3.0,
            socket_timeout=30.0,
            health_check_interval=30,
        )
    return _stream_client


def set_stream_client_for_tests(client: Any) -> None:
    global _stream_client
    _stream_client = client
    _groups_ready.clear()


def queue_for_job_type(job_type: str) -> Tuple[str, str]:
    """Return (stream_key, consumer_group) for a job type."""
    if not multi_queue_enabled():
        return QUEUE_KEY, CONSUMER_GROUP
    if job_type in INGEST_TYPES:
        return QUEUE_INGEST, CONSUMER_GROUP
    if job_type in MEETING_TYPES:
        return QUEUE_MEETING, CONSUMER_GROUP
    if job_type in NOTIFY_TYPES:
        return QUEUE_NOTIFY, CONSUMER_GROUP
    return QUEUE_KEY, CONSUMER_GROUP


def worker_queue_pairs() -> List[Tuple[str, str]]:
    """Streams this worker consumes (priority order), as (stream, group)."""
    mode = (os.getenv("JOB_WORKER_QUEUES") or "all").strip().lower()
    if not multi_queue_enabled() or mode == "default":
        return [(QUEUE_KEY, CONSUMER_GROUP)]
    if mode == "ingest":
        return [(QUEUE_INGEST, CONSUMER_GROUP), (QUEUE_KEY, CONSUMER_GROUP)]
    if mode == "meeting":
        return [(QUEUE_MEETING, CONSUMER_GROUP)]
    if mode == "notify":
        return [(QUEUE_NOTIFY, CONSUMER_GROUP)]
    return [(stream, CONSUMER_GROUP) for stream in ALL_STREAMS]


def build_job(job_type: str, payload: Dict[str, Any], *, job_id: Optional[str] = None, attempts: int = 0) -> Dict[str, Any]:
    stream, _group = queue_for_job_type(job_type)
    return {
        "id": job_id or str(uuid.uuid4()),
        "type": job_type,
        "payload": payload,
        "enqueued_at": datetime.utcnow().isoformat() + "Z",
        "attempts": attempts,
        "queue": stream,
    }


async def _ensure_group(client: Any, stream: str, group: str = CONSUMER_GROUP) -> None:
    key = f"{stream}|{group}"
    if key in _groups_ready:
        return
    try:
        await client.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    _groups_ready.add(key)


async def publish_job(job: Dict[str, Any], *, client: Any = None) -> str:
    """XADD a fully built job. Raises on Redis failure (the outbox relay retries)."""
    client = client or get_stream_client()
    stream = job.get("queue") or queue_for_job_type(str(job.get("type")))[0]
    await _ensure_group(client, stream)
    clean = {k: v for k, v in job.items() if not str(k).startswith("_")}
    return await client.xadd(stream, {"job": json.dumps(clean, ensure_ascii=False)}, maxlen=STREAM_MAXLEN, approximate=True)


async def enqueue_job(job_type: str, payload: Dict[str, Any], *, job_id: Optional[str] = None) -> Optional[str]:
    """Publish a job directly. Returns job_id, or None when Redis is unavailable."""
    job = build_job(job_type, payload, job_id=job_id)
    try:
        await publish_job(job)
        logger.info("Enqueued job %s type=%s stream=%s", job["id"], job_type, job["queue"])
        return job["id"]
    except Exception as exc:
        _note_redis_failure("enqueue", exc)
        return None


def _decode_entries(reply: Any) -> List[Tuple[str, str, Dict[str, str]]]:
    """Normalise XREADGROUP replies (RESP2 list or RESP3 dict) to (stream, id, fields)."""
    out: List[Tuple[str, str, Dict[str, str]]] = []
    if not reply:
        return out
    items: Iterable[Any] = reply.items() if isinstance(reply, dict) else reply
    for stream, entries in items:
        # RESP3 wraps the entry list in one more list.
        if isinstance(reply, dict) and len(entries or []) == 1 and isinstance(entries[0], list):
            entries = entries[0]
        for entry_id, fields in entries or []:
            out.append((stream, entry_id, fields or {}))
    return out


def _job_from_entry(stream: str, entry_id: str, fields: Dict[str, str], *, deliveries: int = 1) -> Dict[str, Any]:
    try:
        job = json.loads(fields.get("job") or "{}")
    except json.JSONDecodeError:
        job = {"type": "invalid", "payload": {"raw": fields}}
    job["_stream"] = stream
    job["_entry_id"] = entry_id
    job["_deliveries"] = deliveries
    job.setdefault("queue", stream)
    return job


async def _times_delivered(client: Any, stream: str, entry_id: str) -> int:
    try:
        rows = await client.xpending_range(stream, CONSUMER_GROUP, min=entry_id, max=entry_id, count=1)
        if rows:
            return int(rows[0].get("times_delivered") or 1)
    except Exception:
        pass
    return 1


async def claim_stale_job(*, max_attempts: int = 3, client: Any = None) -> Optional[Dict[str, Any]]:
    """Reclaim one entry another consumer left pending (crash, OOM, redeploy)."""
    client = client or get_stream_client()
    me = consumer_name()
    for stream, group in worker_queue_pairs():
        await _ensure_group(client, stream, group)
        reply = await client.xautoclaim(stream, group, me, min_idle_time=CLAIM_IDLE_MS, start_id="0-0", count=1)
        claimed = reply[1] if isinstance(reply, (list, tuple)) and len(reply) > 1 else []
        for entry_id, fields in claimed:
            if not fields:
                await client.xack(stream, group, entry_id)
                continue
            deliveries = await _times_delivered(client, stream, entry_id)
            job = _job_from_entry(stream, entry_id, fields, deliveries=deliveries)
            if deliveries > max_attempts:
                logger.error("Job %s exceeded %s deliveries after crashes; dead-lettering", job.get("id"), max_attempts)
                await dead_letter(job, reason="max_deliveries", client=client)
                continue
            logger.warning("Reclaimed stale job %s from %s (delivery %s)", job.get("id"), stream, deliveries)
            return job
    return None


async def dequeue_job(timeout_seconds: int = 5, *, max_attempts: int = 3) -> Optional[Dict[str, Any]]:
    """Reclaim stale work first, then block on new entries across this worker's streams."""
    try:
        client = get_stream_client()
        stale = await claim_stale_job(max_attempts=max_attempts, client=client)
        if stale is not None:
            return stale
        pairs = worker_queue_pairs()
        for stream, group in pairs:
            await _ensure_group(client, stream, group)
        reply = await client.xreadgroup(
            CONSUMER_GROUP,
            consumer_name(),
            {stream: ">" for stream, _group in pairs},
            count=1,
            block=max(1, int(timeout_seconds)) * 1000,
        )
        entries = _decode_entries(reply)
        if not entries:
            return None
        stream, entry_id, fields = entries[0]
        return _job_from_entry(stream, entry_id, fields)
    except Exception as exc:
        _note_redis_failure("dequeue", exc)
        return None


async def ack_job(job: Dict[str, Any], *, client: Any = None) -> None:
    stream = job.get("_stream")
    entry_id = job.get("_entry_id")
    if not stream or not entry_id:
        return
    try:
        client = client or get_stream_client()
        await client.xack(stream, CONSUMER_GROUP, entry_id)
        await client.xdel(stream, entry_id)
    except Exception as exc:
        _note_redis_failure("ack", exc)


async def dead_letter(job: Dict[str, Any], *, reason: str, error: str = "", client: Any = None) -> None:
    client = client or get_stream_client()
    clean = {k: v for k, v in job.items() if not str(k).startswith("_")}
    await client.xadd(
        DEAD_LETTER_KEY,
        {
            "job": json.dumps(clean, ensure_ascii=False),
            "reason": reason,
            "error": (error or "")[:2000],
            "source_stream": str(job.get("_stream") or job.get("queue") or ""),
            "dead_at": datetime.utcnow().isoformat() + "Z",
        },
        maxlen=STREAM_MAXLEN,
        approximate=True,
    )
    await ack_job(job, client=client)


async def requeue_job(job: Dict[str, Any], *, max_attempts: int = 3, error: str = "") -> bool:
    """Retry by re-publishing with attempts+1, or dead-letter at the limit. Returns True if retried."""
    attempts = int(job.get("attempts") or 0) + 1
    job["attempts"] = attempts
    try:
        client = get_stream_client()
        if attempts >= max_attempts:
            logger.error("Dead-lettering job %s after %s attempts", job.get("id"), attempts)
            await dead_letter(job, reason="max_attempts", error=error, client=client)
            return False
        retry = {k: v for k, v in job.items() if not str(k).startswith("_")}
        await publish_job(retry, client=client)
        await ack_job(job, client=client)
        logger.warning("Requeued job %s attempt=%s stream=%s", job.get("id"), attempts, retry.get("queue"))
        return True
    except Exception as exc:
        _note_redis_failure("requeue", exc)
        return False


async def drain_legacy_lists(*, client: Any = None, limit: int = 10000) -> int:
    """Move jobs left on pre-streams Redis lists into their streams."""
    client = client or get_stream_client()
    moved = 0
    for list_key, stream in LEGACY_LIST_KEYS.items():
        for _ in range(limit):
            raw = await client.rpop(list_key)
            if raw is None:
                break
            try:
                job = json.loads(raw)
            except json.JSONDecodeError:
                continue
            job["queue"] = queue_for_job_type(str(job.get("type")))[0] if multi_queue_enabled() else stream
            await publish_job(job, client=client)
            moved += 1
    if moved:
        logger.info("Moved %s job(s) from legacy Redis lists to streams", moved)
    return moved


async def list_dead_letters(limit: int = 50, *, client: Any = None) -> List[Dict[str, Any]]:
    client = client or get_stream_client()
    rows = await client.xrevrange(DEAD_LETTER_KEY, max="+", min="-", count=limit)
    out = []
    for entry_id, fields in rows or []:
        try:
            job = json.loads(fields.get("job") or "{}")
        except json.JSONDecodeError:
            job = {}
        out.append(
            {
                "entry_id": entry_id,
                "job_id": job.get("id"),
                "type": job.get("type"),
                "attempts": job.get("attempts"),
                "reason": fields.get("reason"),
                "error": fields.get("error"),
                "source_stream": fields.get("source_stream"),
                "dead_at": fields.get("dead_at"),
            }
        )
    return out


async def retry_dead_letter(entry_id: str, *, client: Any = None) -> Optional[str]:
    """Re-publish one dead-lettered job with attempts reset. Returns the job id."""
    client = client or get_stream_client()
    rows = await client.xrange(DEAD_LETTER_KEY, min=entry_id, max=entry_id, count=1)
    if not rows:
        return None
    _id, fields = rows[0]
    job = json.loads(fields.get("job") or "{}")
    job["attempts"] = 0
    job["queue"] = queue_for_job_type(str(job.get("type")))[0]
    await publish_job(job, client=client)
    await client.xdel(DEAD_LETTER_KEY, entry_id)
    return job.get("id")


async def stream_stats(*, client: Any = None) -> Dict[str, Dict[str, Any]]:
    """Per-stream length, pending, lag (Redis 7+), and consumers."""
    client = client or get_stream_client()
    stats: Dict[str, Dict[str, Any]] = {}
    for stream in ALL_STREAMS:
        await _ensure_group(client, stream)
        row: Dict[str, Any] = {"length": int(await client.xlen(stream) or 0), "pending": 0, "lag": None, "consumers": 0}
        try:
            for group in await client.xinfo_groups(stream) or []:
                if group.get("name") == CONSUMER_GROUP:
                    row["pending"] = int(group.get("pending") or 0)
                    row["consumers"] = int(group.get("consumers") or 0)
                    lag = group.get("lag")
                    row["lag"] = int(lag) if lag is not None else None
        except Exception as exc:
            logger.debug("XINFO GROUPS failed for %s: %s", stream, exc)
        stats[stream] = row
    stats[DEAD_LETTER_KEY] = {"length": int(await client.xlen(DEAD_LETTER_KEY) or 0)}
    return stats
