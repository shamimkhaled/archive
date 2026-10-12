"""Background worker: consumes Redis Streams through a consumer group."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
import traceback

from ..ai.routing import maybe_refresh_registry
from ..config import load_environment
from ..logging_config import configure_logging
from ..observability import incr, observe_histogram, set_gauge, setup_tracing, start_metrics_server
from .handlers import dispatch_job
from .queue import (
    ack_job,
    consumer_name,
    dequeue_job,
    drain_legacy_lists,
    job_queue_enabled,
    requeue_job,
    worker_queue_pairs,
)
from .runs import mark_failed, mark_running, mark_succeeded

logger = logging.getLogger("bcp_project.jobs.worker")

_shutdown = asyncio.Event()


def _request_shutdown(*_args) -> None:
    _shutdown.set()


async def process_one(job: dict, *, session_factory, max_attempts: int) -> bool:
    """Run one job end to end. Returns True on success."""
    job_id = job.get("id")
    job_type = job.get("type")
    await mark_running(session_factory, job, consumer_name())
    started = time.perf_counter()
    try:
        logger.info("Processing job %s type=%s", job_id, job_type)
        await dispatch_job(job)
    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        retried = await requeue_job(job, max_attempts=max_attempts, error=error)
        await mark_failed(session_factory, job, error, dead=not retried)
        _record_job(job_type, started, "retried" if retried else "dead")
        return False
    await ack_job(job)
    await mark_succeeded(session_factory, job)
    _record_job(job_type, started, "ok")
    return True


def _record_job(job_type: str | None, started: float, outcome: str) -> None:
    labels = {"job_type": job_type or "unknown"}
    observe_histogram("job_duration", time.perf_counter() - started, labels)
    incr("job_runs", labels={**labels, "outcome": outcome})


async def run_worker_loop(*, poll_timeout: int = 5, max_attempts: int = 3) -> None:
    load_environment()
    from ..db import get_session

    if not job_queue_enabled():
        logger.warning(
            "ENABLE_JOB_QUEUE is not set; worker will still consume streams. "
            "Set ENABLE_JOB_QUEUE=1 on the API so uploads enqueue jobs."
        )
    try:
        await drain_legacy_lists()
    except Exception as exc:
        logger.warning("Legacy list drain skipped: %s", exc)
    streams = ", ".join(stream for stream, _group in worker_queue_pairs())
    logger.info("Job worker %s consuming [%s] (block=%ss)", consumer_name(), streams, poll_timeout)
    queues = os.getenv("JOB_WORKER_QUEUES", "all")
    while not _shutdown.is_set():
        set_gauge("job_worker_heartbeat_timestamp", time.time(), {"queues": queues})
        job = await dequeue_job(timeout_seconds=poll_timeout, max_attempts=max_attempts)
        if job is None:
            continue
        await maybe_refresh_registry(get_session)
        await process_one(job, session_factory=get_session, max_attempts=max_attempts)
    logger.info("Job worker shut down")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="BCP background job worker")
    parser.add_argument("--poll-timeout", type=int, default=int(os.getenv("JOB_POLL_TIMEOUT", "5")))
    parser.add_argument("--max-attempts", type=int, default=int(os.getenv("JOB_MAX_ATTEMPTS", "3")))
    args = parser.parse_args(argv)

    os.environ.setdefault("SERVICE_NAME", f"worker-{os.getenv('JOB_WORKER_QUEUES', 'all')}")
    configure_logging()
    setup_tracing()
    start_metrics_server()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _request_shutdown())

    try:
        loop.run_until_complete(run_worker_loop(poll_timeout=args.poll_timeout, max_attempts=args.max_attempts))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
