"""Lightweight observability: in-process counters, latency histograms, Prometheus text, and optional OpenTelemetry."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger("bcp_project.observability")

Labels = Tuple[Tuple[str, str], ...]

_lock = Lock()
_counters: Dict[Tuple[str, Labels], float] = defaultdict(float)
_timings_ms: Dict[str, list[float]] = defaultdict(list)

# Prometheus histogram buckets (seconds) for HTTP, AI, and job latency (OCR jobs run for minutes).
BUCKETS: Tuple[float, ...] = (0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 900.0)
_hist_counts: Dict[Tuple[str, Labels], list[int]] = {}
_hist_sum: Dict[Tuple[str, Labels], float] = defaultdict(float)
_gauges: Dict[Tuple[str, Labels], float] = {}

_TIMINGS_KEEP = 200
_FLUSH_INTERVAL_S = 5.0
_PROCESS_ID = f"{os.getpid()}-{int(time.time() * 1000)}"
_last_flush = 0.0
_flush_lock = Lock()
_flush_timer: Optional[threading.Timer] = None


def _key(name: str, labels: Optional[Dict[str, str]]) -> Tuple[str, Labels]:
    return name, tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))


def incr(metric: str, amount: float = 1, labels: Optional[Dict[str, str]] = None) -> None:
    with _lock:
        _counters[_key(metric, labels)] += amount


def observe_ms(metric: str, duration_ms: float, *, keep: int = _TIMINGS_KEEP) -> None:
    with _lock:
        bucket = _timings_ms[metric]
        bucket.append(duration_ms)
        if len(bucket) > keep:
            del bucket[: len(bucket) - keep]


def observe_histogram(name: str, seconds: float, labels: Optional[Dict[str, str]] = None) -> None:
    key = _key(name, labels)
    with _lock:
        counts = _hist_counts.setdefault(key, [0] * (len(BUCKETS) + 1))
        for idx, bound in enumerate(BUCKETS):
            if seconds <= bound:
                counts[idx] += 1
        counts[-1] += 1
        _hist_sum[key] += seconds


def set_gauge(name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
    with _lock:
        _gauges[_key(name, labels)] = float(value)


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


def _counter_label(name: str, pairs: Labels) -> str:
    if not pairs:
        return name
    return name + "{" + ",".join(f"{k}={v}" for k, v in pairs) + "}"


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
        counters = {_counter_label(name, pairs): value for (name, pairs), value in _counters.items()}
        return {"counters": counters, "timings": timings}


# --- Cross-process aggregation ------------------------------------------------
#
# Uvicorn runs WEB_CONCURRENCY processes per container and a scrape lands on one
# of them. With METRICS_MULTIPROC_DIR set, each process writes its state to that
# directory and /metrics sums every file, so counters stay monotonic.


def _multiproc_dir() -> Optional[Path]:
    raw = (os.getenv("METRICS_MULTIPROC_DIR") or "").strip()
    return Path(raw) if raw else None


def _local_state() -> Dict[str, Any]:
    with _lock:
        return {
            "counters": [[n, list(map(list, p)), v] for (n, p), v in _counters.items()],
            "hist": [[n, list(map(list, p)), list(c), _hist_sum[(n, p)]] for (n, p), c in _hist_counts.items()],
            "timings": {k: list(v) for k, v in _timings_ms.items()},
        }


def _deferred_flush() -> None:
    global _flush_timer
    with _flush_lock:
        _flush_timer = None
    flush_state(force=True)


def flush_state(*, force: bool = False) -> None:
    """Write this process's metrics to METRICS_MULTIPROC_DIR (throttled unless ``force``).

    A throttled call schedules one deferred write, so the last requests before a
    process goes idle still reach the merged /metrics.
    """
    global _last_flush, _flush_timer
    directory = _multiproc_dir()
    if directory is None:
        return
    now = time.monotonic()
    if not force and now - _last_flush < _FLUSH_INTERVAL_S:
        with _flush_lock:
            if _flush_timer is None:
                _flush_timer = threading.Timer(_FLUSH_INTERVAL_S - (now - _last_flush), _deferred_flush)
                _flush_timer.daemon = True
                _flush_timer.start()
        return
    _last_flush = now
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{_PROCESS_ID}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(_local_state()), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        logger.debug("metrics flush failed: %s", exc)


def _merged_state() -> Dict[str, Any]:
    directory = _multiproc_dir()
    if directory is None:
        return _local_state()
    flush_state(force=True)
    counters: Dict[Tuple[str, Labels], float] = defaultdict(float)
    hist_counts: Dict[Tuple[str, Labels], list[int]] = {}
    hist_sum: Dict[Tuple[str, Labels], float] = defaultdict(float)
    timings: Dict[str, list[float]] = defaultdict(list)
    for path in sorted(directory.glob("*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for name, pairs, value in state.get("counters", []):
            counters[(name, tuple(tuple(p) for p in pairs))] += value
        for name, pairs, counts, total in state.get("hist", []):
            key = (name, tuple(tuple(p) for p in pairs))
            merged = hist_counts.setdefault(key, [0] * len(counts))
            for idx, value in enumerate(counts[: len(merged)]):
                merged[idx] += value
            hist_sum[key] += total
        for name, values in state.get("timings", {}).items():
            timings[name].extend(values)
    return {
        "counters": [[n, p, v] for (n, p), v in counters.items()],
        "hist": [[n, p, c, hist_sum[(n, p)]] for (n, p), c in hist_counts.items()],
        "timings": {k: v[-_TIMINGS_KEEP * 8 :] for k, v in timings.items()},
    }


_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def _metric_name(raw: str) -> str:
    return "bcp_" + _NAME_RE.sub("_", raw).strip("_")


def _labels(pairs: Iterable[Iterable[str]], extra: Optional[Dict[str, str]] = None) -> str:
    items = [tuple(p) for p in pairs] + sorted((extra or {}).items())
    if not items:
        return ""
    body = ",".join(f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"' for k, v in items)
    return "{" + body + "}"


def _num(value: float) -> str:
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


def render_prometheus() -> str:
    """Render all metrics in Prometheus text exposition format 0.0.4."""
    state = _merged_state()
    lines: list[str] = []
    seen: set[str] = set()

    def _type(name: str, kind: str, raw: str) -> None:
        if name not in seen:
            lines.append(f"# HELP {name} {raw}")
            lines.append(f"# TYPE {name} {kind}")
            seen.add(name)

    for raw, pairs, value in sorted(state["counters"], key=lambda r: (r[0], r[1])):
        name = _metric_name(raw) + "_total"
        _type(name, "counter", raw)
        lines.append(f"{name}{_labels(pairs)} {_num(value)}")
    for raw, values in sorted(state["timings"].items()):
        if not values:
            continue
        ordered = sorted(values)
        name = _metric_name(raw) + "_seconds"
        _type(name, "summary", f"{raw} (recent samples)")
        for q in (0.5, 0.95, 0.99):
            idx = min(len(ordered) - 1, max(0, int(len(ordered) * q) - 1))
            lines.append(f'{name}{{quantile="{q}"}} {ordered[idx] / 1000.0:.6f}')
        lines.append(f"{name}_count {len(ordered)}")
        lines.append(f"{name}_sum {sum(ordered) / 1000.0:.6f}")
    for raw, pairs, counts, total in sorted(state["hist"], key=lambda r: (r[0], r[1])):
        name = _metric_name(raw) + "_seconds"
        _type(name, "histogram", raw)
        for idx, bound in enumerate(BUCKETS):
            lines.append(f"{name}_bucket{_labels(pairs, {'le': str(bound)})} {counts[idx]}")
        lines.append(f"{name}_bucket{_labels(pairs, {'le': '+Inf'})} {counts[-1]}")
        lines.append(f"{name}_count{_labels(pairs)} {counts[-1]}")
        lines.append(f"{name}_sum{_labels(pairs)} {total:.6f}")
    with _lock:
        gauges = sorted(_gauges.items())
    for (raw, pairs), value in gauges:
        name = _metric_name(raw)
        _type(name, "gauge", raw)
        lines.append(f"{name}{_labels(pairs)} {_num(value)}")
    return "\n".join(lines) + "\n"


def reset_metrics_for_tests() -> None:
    global _flush_timer, _last_flush
    with _flush_lock:
        if _flush_timer is not None:
            _flush_timer.cancel()
            _flush_timer = None
    _last_flush = 0.0
    with _lock:
        _counters.clear()
        _timings_ms.clear()
        _hist_counts.clear()
        _hist_sum.clear()
        _gauges.clear()


def metrics_access(authorization: Optional[str]) -> Optional[int]:
    """None when a scrape may proceed, else the HTTP status to return.

    METRICS_TOKEN requires ``Authorization: Bearer <token>``. Without a token the
    endpoint is open in development and hidden (404) in production.
    """
    from .config import is_production

    expected = (os.getenv("METRICS_TOKEN") or "").strip()
    if expected:
        provided = (authorization or "").removeprefix("Bearer ").strip()
        return None if hmac.compare_digest(provided, expected) else 401
    return 404 if is_production() else None


def start_metrics_server(port: Optional[int] = None) -> Optional[Any]:
    """Serve /metrics on METRICS_PORT for processes without an HTTP app (workers, relay)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from .config import is_production

    if port is None:
        port = int(os.getenv("METRICS_PORT") or 0)
        if not port:
            return None
    if is_production() and not (os.getenv("METRICS_TOKEN") or "").strip():
        logger.warning("METRICS_PORT is set but METRICS_TOKEN is not; worker metrics server not started")
        return None

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/metrics":
                self.send_error(404)
                return
            denied = metrics_access(self.headers.get("Authorization"))
            if denied:
                self.send_error(denied)
                return
            body = render_prometheus().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            return None

    server = ThreadingHTTPServer((os.getenv("METRICS_HOST", "0.0.0.0"), port), _Handler)
    threading.Thread(target=server.serve_forever, name="metrics-server", daemon=True).start()
    logger.info("Metrics server listening on :%s/metrics", server.server_address[1])
    return server


class MetricsMiddleware:
    """Pure ASGI middleware: request count and latency by route template (not raw path)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status_holder = {"code": 500}

        async def _send(message):
            if message.get("type") == "http.response.start":
                status_holder["code"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None)
            if template is None:
                path = scope.get("path") or ""
                template = "/static" if path.startswith("/static/") else "unmatched"
            labels = {
                "method": scope.get("method", "GET"),
                "route": template,
                "status": f"{status_holder['code'] // 100}xx",
            }
            observe_histogram("http_request_duration", time.perf_counter() - start, labels)
            flush_state()


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


def setup_tracing(app=None) -> bool:
    """Enable OTLP tracing when OTEL_EXPORTER_OTLP_ENDPOINT is set and the SDK is installed."""
    endpoint = (os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    if not endpoint:
        return False
    try:
        from opentelemetry import trace  # type: ignore
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,  # type: ignore
        )
        from opentelemetry.sdk.resources import Resource  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore
    except ImportError:
        logger.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry-sdk is not installed")
        return False

    from .logging_config import service_name

    provider = TracerProvider(resource=Resource.create({"service.name": f"sonali-archive-{service_name()}"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # type: ignore

            FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics,static")
        except ImportError:
            logger.info("opentelemetry-instrumentation-fastapi not installed; manual spans only")
    logger.info("OpenTelemetry tracing enabled endpoint=%s", endpoint)
    return True
