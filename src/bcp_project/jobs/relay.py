"""Outbox relay process: ``python -m bcp_project.jobs.relay``."""

from __future__ import annotations

import asyncio
import os
import signal

from ..config import load_environment
from ..logging_config import configure_logging
from ..observability import setup_tracing, start_metrics_server


def main() -> None:
    load_environment()
    os.environ.setdefault("SERVICE_NAME", "outbox-relay")
    configure_logging()
    setup_tracing()
    start_metrics_server()

    from ..db import get_session
    from .outbox import run_relay_loop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop.set())
    try:
        loop.run_until_complete(run_relay_loop(get_session, stop))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
