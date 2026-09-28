"""Background worker process for Redis job queue."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

from ..config import load_environment
from .handlers import dispatch_job
from .queue import ack_job, dequeue_job, job_queue_enabled, requeue_job

logger = logging.getLogger("bcp_project.jobs.worker")

_shutdown = asyncio.Event()


def _request_shutdown(*_args) -> None:
    _shutdown.set()


async def run_worker_loop(*, poll_timeout: int = 5, max_attempts: int = 3) -> None:
    load_environment()
    if not job_queue_enabled():
        logger.warning(
            "ENABLE_JOB_QUEUE is not set; worker will still poll Redis. "
            "Set ENABLE_JOB_QUEUE=1 on the API so uploads enqueue jobs."
        )
    logger.info("Job worker started (poll_timeout=%ss)", poll_timeout)
    while not _shutdown.is_set():
        job = await dequeue_job(timeout_seconds=poll_timeout)
        if job is None:
            continue
        job_id = job.get("id")
        job_type = job.get("type")
        try:
            logger.info("Processing job %s type=%s", job_id, job_type)
            await dispatch_job(job)
            await ack_job(job)
            logger.info("Acked job %s", job_id)
        except Exception:
            logger.exception("Job %s failed", job_id)
            await requeue_job(job, max_attempts=max_attempts)
    logger.info("Job worker shut down")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="BCP background job worker")
    parser.add_argument("--poll-timeout", type=int, default=int(os.getenv("JOB_POLL_TIMEOUT", "5")))
    parser.add_argument("--max-attempts", type=int, default=int(os.getenv("JOB_MAX_ATTEMPTS", "3")))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

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
