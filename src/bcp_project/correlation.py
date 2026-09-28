"""Request correlation IDs (contextvar + middleware)."""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

CORRELATION_HEADER = "X-Correlation-ID"

_correlation_id: ContextVar[Optional[str]] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> Optional[str]:
    return _correlation_id.get()


def set_correlation_id(value: Optional[str]) -> None:
    _correlation_id.set(value)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = (request.headers.get(CORRELATION_HEADER) or "").strip()
        cid = incoming if incoming and len(incoming) <= 128 else new_correlation_id()
        token = _correlation_id.set(cid)
        request.state.correlation_id = cid
        try:
            response: Response = await call_next(request)
        finally:
            _correlation_id.reset(token)
        response.headers[CORRELATION_HEADER] = cid
        return response
