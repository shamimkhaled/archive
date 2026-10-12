#!/usr/bin/env python3
"""Scale the ingest and meeting worker services on Railway from Redis Streams backlog.

Dry run by default (prints decisions). Run as its own small Railway service, or a cron service with --once:

  PYTHONPATH=src python scripts/autoscale_workers.py                     # dry run, every 60 s
  RAILWAY_API_TOKEN=... RAILWAY_ENVIRONMENT_ID=... \
  RAILWAY_INGEST_WORKER_SERVICE_ID=... RAILWAY_MEETING_WORKER_SERVICE_ID=... \
    PYTHONPATH=src python scripts/autoscale_workers.py --apply

Workers are safe to add or remove at any time: unacknowledged jobs on a stopped replica are
reclaimed by the others (XAUTOCLAIM), and every handler is idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bcp_project.config import load_environment

load_environment()

from bcp_project.jobs.autoscale import RailwayClient, autoscale_once, load_pools
from bcp_project.jobs.queue import stream_stats


async def run(args: argparse.Namespace) -> int:
    pools = load_pools()
    token, env_id = os.getenv("RAILWAY_API_TOKEN", ""), os.getenv("RAILWAY_ENVIRONMENT_ID", "")
    railway = RailwayClient(token, env_id) if token and env_id else None
    if args.apply and railway is None:
        print("--apply needs RAILWAY_API_TOKEN and RAILWAY_ENVIRONMENT_ID", file=sys.stderr)
        return 2
    state: dict = {}
    while True:
        try:
            decisions = await autoscale_once(
                stats=await stream_stats(), pools=pools, railway=railway, state=state, apply=args.apply
            )
            print(json.dumps(decisions))
        except Exception as exc:  # keep the loop alive through Redis or Railway blips
            print(f"autoscale pass failed: {exc}", file=sys.stderr)
        if args.once:
            return 0
        await asyncio.sleep(args.interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="Change replica counts (default: dry run)")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=int(os.getenv("AUTOSCALE_INTERVAL_SECONDS", "60")))
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
