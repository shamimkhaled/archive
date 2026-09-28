"""Durable background jobs (Redis queue + worker)."""

from .handlers import (
    JOB_CHUNK_INDEX,
    JOB_MEETING_CHUNK_STT,
    JOB_MEETING_MINUTES,
    JOB_MEETING_TRANSCRIBE,
    dispatch_job,
    run_chunk_index,
    run_meeting_chunk_stt,
    run_meeting_minutes,
    run_meeting_transcribe,
)
from .queue import enqueue_job, job_queue_enabled
from .worker import main as worker_main
from .worker import run_worker_loop

__all__ = [
    "JOB_CHUNK_INDEX",
    "JOB_MEETING_CHUNK_STT",
    "JOB_MEETING_MINUTES",
    "JOB_MEETING_TRANSCRIBE",
    "dispatch_job",
    "enqueue_job",
    "job_queue_enabled",
    "run_chunk_index",
    "run_meeting_chunk_stt",
    "run_meeting_minutes",
    "run_meeting_transcribe",
    "run_worker_loop",
    "worker_main",
]
