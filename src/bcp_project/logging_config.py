"""Process-wide logging: JSON lines in production, readable text in development.

Every record carries the request correlation ID and the service name so logs
from web, relay, and workers can be joined in one search.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

from .correlation import get_correlation_id

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


def service_name() -> str:
    return (os.getenv("SERVICE_NAME") or os.getenv("RAILWAY_SERVICE_NAME") or "web").strip() or "web"


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "correlation_id", None):
            record.correlation_id = get_correlation_id() or "-"
        record.service = service_name()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": getattr(record, "service", service_name()),
            "correlation_id": getattr(record, "correlation_id", None),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key in payload or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except TypeError:
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _use_json() -> bool:
    explicit = (os.getenv("LOG_FORMAT") or "").strip().lower()
    if explicit:
        return explicit == "json"
    from .config import is_production

    return is_production()


_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    level = (os.getenv("LOG_LEVEL") or "INFO").upper()
    handler = logging.StreamHandler()
    handler.addFilter(CorrelationFilter())
    if _use_json():
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(service)s %(name)s cid=%(correlation_id)s] %(message)s")
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
