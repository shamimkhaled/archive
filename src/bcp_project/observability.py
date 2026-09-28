"""Lightweight observability: in-process counters + optional OpenTelemetry hooks."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from threading import Lock
from typing import Any, Dict, Optional

logger = logging.getLogger("bcp_project.observability")

_lock = Lock()
_counters: Dict[str, int] = defaultdict(int)
_timings_ms: Dict[str, list[float]] = defaultdict(list)


def incr(metric: str, amount: int = 1) -> None:
    with _lock:
        _counters[metric] += amount


def observe_ms(metric: str, duration_ms: float, *, keep: int = 200) -> None:
    with _lock:
        bucket = _timings_ms[metric]
        bucket.append(duration_ms)
        if len(bucket) > keep:
            del bucket[: len(bucket) - keep]


class Timer:
    def __init__(self, metric: str):
        self.metric = metric
        self._start = 0.0

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        elapsed = (time.perf_counter() - self._start) * 1000.0
        observe_ms(self.metric, elapsed)
        incr(f"{self.metric}.count")


def snapshot_metrics() -> Dict[str, Any]:
    with _lock:
        timings = {}
        for key, values in _timings_ms.items():
            if not values:
                continue
            ordered = sorted(values)
            timings[key] = {
                "count": len(ordered),
                "p50_ms": ordered[len(ordered) // 2],
                "p95_ms": ordered[max(0, int(len(ordered) * 0.95) - 1)],
                "max_ms": ordered[-1],
            }
        return {"counters": dict(_counters), "timings": timings}


def try_start_span(name: str, attributes: Optional[Dict[str, Any]] = None):
    """No-op span context if OpenTelemetry is not installed."""
    try:
        from opentelemetry import trace  # type: ignore

        tracer = trace.get_tracer("bcp_project")
        return tracer.start_as_current_span(name, attributes=attributes or {})
    except Exception:
        return _NullSpan()


class _NullSpan:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_attribute(self, *_args, **_kwargs):
        return None
