"""Scale worker services on Railway by Redis Streams backlog.

Backlog per queue is entries not yet delivered to the consumer group (lag, Redis 7+; stream
length otherwise) plus entries delivered but not acknowledged. Scale-up is immediate; scale-down
waits for ``scale_down_after`` consecutive quiet checks so a burst does not flap replicas.

Pools come from ``AUTOSCALE_POOLS`` (JSON list) or default to the ingest and meeting workers:
  [{"name": "ingest", "stream": "bcp:stream:ingest", "service_id": "<railway service id>",
    "min": 1, "max": 4, "jobs_per_replica": 20}]
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen

from .queue import QUEUE_INGEST, QUEUE_MEETING

logger = logging.getLogger("bcp_project.jobs.autoscale")

RAILWAY_API = os.getenv("RAILWAY_API_URL", "https://backboard.railway.com/graphql/v2")


@dataclass
class Pool:
    name: str
    stream: str
    service_id: str
    min_replicas: int = 1
    max_replicas: int = 4
    jobs_per_replica: int = 20
    scale_down_after: int = 5


def load_pools() -> List[Pool]:
    raw = (os.getenv("AUTOSCALE_POOLS") or "").strip()
    if raw:
        return [
            Pool(
                name=p["name"],
                stream=p["stream"],
                service_id=p.get("service_id", ""),
                min_replicas=int(p.get("min", 1)),
                max_replicas=int(p.get("max", 4)),
                jobs_per_replica=int(p.get("jobs_per_replica", 20)),
                scale_down_after=int(p.get("scale_down_after", 5)),
            )
            for p in json.loads(raw)
        ]
    return [
        Pool("ingest", QUEUE_INGEST, os.getenv("RAILWAY_INGEST_WORKER_SERVICE_ID", ""), 1, 4, 20),
        # One transcription per replica keeps a long meeting from blocking the next.
        Pool("meeting", QUEUE_MEETING, os.getenv("RAILWAY_MEETING_WORKER_SERVICE_ID", ""), 1, 3, 2),
    ]


def backlog(stats: Dict[str, Any]) -> int:
    lag = stats.get("lag")
    waiting = int(lag) if lag is not None else int(stats.get("length") or 0)
    return waiting + int(stats.get("pending") or 0)


def desired_replicas(pool: Pool, jobs: int, current: int, quiet_streak: int) -> int:
    """Target replicas for ``jobs`` outstanding; only shrinks after a quiet streak."""
    target = max(pool.min_replicas, min(pool.max_replicas, math.ceil(jobs / max(1, pool.jobs_per_replica))))
    if target >= current:
        return target
    return target if quiet_streak >= pool.scale_down_after else current


class RailwayClient:
    def __init__(self, token: str, environment_id: str):
        self.token, self.environment_id = token, environment_id

    def _gql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        req = Request(
            RAILWAY_API,
            data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("errors"):
            raise RuntimeError(f"Railway API: {payload['errors']}")
        return payload.get("data") or {}

    def replicas(self, service_id: str) -> Optional[int]:
        data = self._gql(
            "query($s: String!, $e: String!) { serviceInstance(serviceId: $s, environmentId: $e) { numReplicas } }",
            {"s": service_id, "e": self.environment_id},
        )
        value = (data.get("serviceInstance") or {}).get("numReplicas")
        return int(value) if value is not None else None

    def set_replicas(self, service_id: str, replicas: int) -> None:
        self._gql(
            "mutation($s: String!, $e: String!, $n: Int!) {"
            " serviceInstanceUpdate(serviceId: $s, environmentId: $e, input: {numReplicas: $n}) }",
            {"s": service_id, "e": self.environment_id, "n": replicas},
        )


async def autoscale_once(
    *,
    stats: Dict[str, Dict[str, Any]],
    pools: List[Pool],
    railway: Optional[RailwayClient],
    state: Dict[str, int],
    apply: bool,
) -> List[Dict[str, Any]]:
    """One decision pass. ``state`` holds each pool's quiet streak and last replica count between passes."""
    decisions = []
    for pool in pools:
        jobs = backlog(stats.get(pool.stream) or {})
        current = state.get(f"{pool.name}:replicas")
        if current is None and railway and pool.service_id:
            current = railway.replicas(pool.service_id)
        current = current if current is not None else pool.min_replicas
        quiet_key = f"{pool.name}:quiet"
        needed = max(pool.min_replicas, math.ceil(jobs / max(1, pool.jobs_per_replica)))
        state[quiet_key] = state.get(quiet_key, 0) + 1 if needed < current else 0
        target = desired_replicas(pool, jobs, current, state[quiet_key])
        decision = {"pool": pool.name, "backlog": jobs, "current": current, "target": target, "applied": False}
        if target != current and apply and railway and pool.service_id:
            railway.set_replicas(pool.service_id, target)
            decision["applied"] = True
            state[quiet_key] = 0
        if target != current:
            logger.info(
                "autoscale %s: backlog=%s replicas %s -> %s%s",
                pool.name, jobs, current, target, "" if decision["applied"] else " (dry run)",
            )
        state[f"{pool.name}:replicas"] = target if decision["applied"] else current
        decisions.append(decision)
    return decisions
