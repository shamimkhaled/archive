"""Redis multi-queue job system (ingest / meeting / notify / default)."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..cache import _note_redis_failure, get_redis_client

logger = logging.getLogger("bcp_project.jobs.queue")

QUEUE_KEY = os.getenv("JOB_QUEUE_KEY", "bcp:jobs")
PROCESSING_KEY = os.getenv("JOB_PROCESSING_KEY", "bcp:jobs:processing")
DEAD_LETTER_KEY = os.getenv("JOB_DEAD_LETTER_KEY", "bcp:jobs:dead")

QUEUE_INGEST = os.getenv("JOB_QUEUE_INGEST", "bcp:jobs:ingest")
QUEUE_MEETING = os.getenv("JOB_QUEUE_MEETING", "bcp:jobs:meeting")
QUEUE_NOTIFY = os.getenv("JOB_QUEUE_NOTIFY", "bcp:jobs:notify")

PROCESSING_INGEST = os.getenv("JOB_PROCESSING_INGEST", "bcp:jobs:ingest:processing")
PROCESSING_MEETING = os.getenv("JOB_PROCESSING_MEETING", "bcp:jobs:meeting:processing")
PROCESSING_NOTIFY = os.getenv("JOB_PROCESSING_NOTIFY", "bcp:jobs:notify:processing")

INGEST_TYPES = frozenset({"chunk_index", "connector_pull"})
MEETING_TYPES = frozenset({"meeting_transcribe", "meeting_chunk_stt", "meeting_minutes"})
NOTIFY_TYPES = frozenset({"webhook_dispatch", "audit_export"})


def job_queue_enabled() -> bool:
    return (os.getenv("ENABLE_JOB_QUEUE") or "").strip().lower() in {"1", "true", "yes", "on"}


def multi_queue_enabled() -> bool:
    return (os.getenv("ENABLE_MULTI_QUEUE") or "1").strip().lower() in {"1", "true", "yes", "on"}


def queue_for_job_type(job_type: str) -> Tuple[str, str]:
    """Return (queue_key, processing_key) for a job type."""
    if not multi_queue_enabled():
        return QUEUE_KEY, PROCESSING_KEY
    if job_type in INGEST_TYPES:
        return QUEUE_INGEST, PROCESSING_INGEST
    if job_type in MEETING_TYPES:
        return QUEUE_MEETING, PROCESSING_MEETING
    if job_type in NOTIFY_TYPES:
        return QUEUE_NOTIFY, PROCESSING_NOTIFY
    return QUEUE_KEY, PROCESSING_KEY


def worker_queue_pairs() -> List[Tuple[str, str]]:
    """Queues this worker should poll (priority order)."""
    mode = (os.getenv("JOB_WORKER_QUEUES") or "all").strip().lower()
    pairs = [
        (QUEUE_MEETING, PROCESSING_MEETING),
        (QUEUE_INGEST, PROCESSING_INGEST),
        (QUEUE_NOTIFY, PROCESSING_NOTIFY),
        (QUEUE_KEY, PROCESSING_KEY),
    ]
    if not multi_queue_enabled() or mode == "default":
        return [(QUEUE_KEY, PROCESSING_KEY)]
    if mode == "ingest":
        return [(QUEUE_INGEST, PROCESSING_INGEST), (QUEUE_KEY, PROCESSING_KEY)]
    if mode == "meeting":
        return [(QUEUE_MEETING, PROCESSING_MEETING)]
    if mode == "notify":
        return [(QUEUE_NOTIFY, PROCESSING_NOTIFY)]
    return pairs


async def enqueue_job(job_type: str, payload: Dict[str, Any], *, job_id: Optional[str] = None) -> Optional[str]:
    """Push a job onto the typed Redis queue. Returns job_id or None if Redis unavailable."""
    queue_key, _processing = queue_for_job_type(job_type)
    job = {
        "id": job_id or str(uuid.uuid4()),
        "type": job_type,
        "payload": payload,
        "enqueued_at": datetime.utcnow().isoformat() + "Z",
        "attempts": 0,
        "queue": queue_key,
    }
    try:
        client = get_redis_client()
        await client.lpush(queue_key, json.dumps(job))
        logger.info("Enqueued job %s type=%s queue=%s", job["id"], job_type, queue_key)
        return job["id"]
    except Exception as exc:
        _note_redis_failure("enqueue", exc)
        return None


async def dequeue_job(timeout_seconds: int = 5) -> Optional[Dict[str, Any]]:
    """Poll configured queues via BRPOPLPUSH round-robin with short timeouts."""
    pairs = worker_queue_pairs()
    # Spread timeout across queues (min 1s each).
    per = max(1, int(timeout_seconds // max(1, len(pairs))))
    try:
        client = get_redis_client()
        for queue_key, processing_key in pairs:
            item = await client.brpoplpush(queue_key, processing_key, timeout=per)
            if item is None:
                continue
            job = json.loads(item)
            job["_raw"] = item
            job["_queue_key"] = queue_key
            job["_processing_key"] = processing_key
            return job
        return None
    except Exception as exc:
        _note_redis_failure("dequeue", exc)
        return None


async def ack_job(job: Dict[str, Any]) -> None:
    raw = job.get("_raw")
    processing_key = job.get("_processing_key") or PROCESSING_KEY
    if not raw:
        return
    try:
        client = get_redis_client()
        await client.lrem(processing_key, 1, raw)
    except Exception as exc:
        _note_redis_failure("ack", exc)


async def requeue_job(job: Dict[str, Any], *, max_attempts: int = 3) -> bool:
    """Move failed job back to its queue or dead-letter after max attempts."""
    raw = job.get("_raw")
    attempts = int(job.get("attempts") or 0) + 1
    job["attempts"] = attempts
    queue_key = job.get("_queue_key") or job.get("queue") or QUEUE_KEY
    processing_key = job.get("_processing_key") or PROCESSING_KEY
    try:
        client = get_redis_client()
        if raw:
            await client.lrem(processing_key, 1, raw)
        dead = {k: v for k, v in job.items() if not str(k).startswith("_")}
        if attempts >= max_attempts:
            logger.error("Dead-lettering job %s after %s attempts", job.get("id"), attempts)
            await client.lpush(DEAD_LETTER_KEY, json.dumps(dead))
            return False
        await client.lpush(queue_key, json.dumps(dead))
        logger.warning("Requeued job %s attempt=%s queue=%s", job.get("id"), attempts, queue_key)
        return True
    except Exception as exc:
        _note_redis_failure("requeue", exc)
        return False
