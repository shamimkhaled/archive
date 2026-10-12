"""AI provider port. Every model call in the app goes through these functions.

Callers pass the task name and the sensitivity of the content; this module picks
the provider (env, or the admin registry), enforces the egress policy, and
records which provider actually served the call.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from ..observability import incr, observe_histogram
from . import egress
from .egress import EgressBlocked, egress_policy, max_sensitivity
from .providers import (
    chat_provider,
    embedding_provider,
    is_cloud,
    rerank_provider,
    stt_provider,
)
from .routing import (
    Route,
    chat_route,
    describe_routes,
    embed_dimension,
    embed_route,
    local_fallback_route,
    maybe_refresh_registry,
    refresh_registry,
    rerank_route,
    stt_route,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EgressBlocked",
    "chat",
    "chat_json",
    "describe_routes",
    "egress_policy",
    "embed",
    "embed_dimension",
    "embed_route",
    "last_call",
    "max_sensitivity",
    "maybe_refresh_registry",
    "refresh_registry",
    "rerank",
    "transcribe",
]

_last_call: ContextVar[Optional[Dict[str, Any]]] = ContextVar("ai_last_call", default=None)


def last_call() -> Optional[Dict[str, Any]]:
    """Provider, model, and task of the most recent call in this context, for provenance."""
    return _last_call.get()


def _resolve(route: Route, sensitivity: Optional[str]) -> Route:
    if egress.allows(route.provider, sensitivity):
        return route
    fallback = local_fallback_route(route.task)
    incr("ai_egress_blocked", labels={"task": route.task, "action": "local_fallback" if fallback else "blocked"})
    if fallback is None:
        raise EgressBlocked(route.task, route.provider, egress.max_sensitivity([sensitivity]))
    logger.info("AI egress: %s rerouted from %s to local (%s)", route.task, route.provider, sensitivity)
    return fallback


def chat_allowed(task: str, sensitivity: Optional[str]) -> bool:
    """True when content at ``sensitivity`` can be sent for ``task`` (directly or via local fallback)."""
    route = chat_route(task)
    return egress.allows(route.provider, sensitivity) or local_fallback_route(route.task) is not None


def _record(route: Route, started: float, ok: bool) -> None:
    labels = {"task": route.task, "provider": route.provider, "status": "ok" if ok else "error"}
    observe_histogram("ai_request_seconds", time.perf_counter() - started, labels)
    if ok:
        _last_call.set({"task": route.task, "provider": route.provider, "model": route.model, "source": route.source})


def chat(
    task: str,
    messages: List[Dict[str, str]],
    *,
    sensitivity: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 1500,
    json_mode: bool = False,
) -> str:
    route = _resolve(chat_route(task), sensitivity)
    started = time.perf_counter()
    try:
        text = chat_provider(route.provider).complete(
            model=route.model, messages=messages, temperature=temperature, max_tokens=max_tokens, json_mode=json_mode
        )
    except Exception:
        _record(route, started, False)
        raise
    _record(route, started, True)
    return text


def parse_json_text(raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise


def chat_json(
    task: str,
    system: str,
    user: str,
    *,
    sensitivity: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 1500,
) -> Dict[str, Any]:
    raw = chat(
        task,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        sensitivity=sensitivity,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=True,
    )
    return parse_json_text(raw)


def embed(inputs: List[str], *, sensitivity: Optional[str] = None, route: Optional[Route] = None) -> List[List[float]]:
    """Embed with the active embedding space (or ``route``, used by the reindex script).
    Never silently switches model: a blocked cloud call raises EgressBlocked."""
    route = route or embed_route()
    if not egress.allows(route.provider, sensitivity):
        incr("ai_egress_blocked", labels={"task": "embed", "action": "blocked"})
        raise EgressBlocked("embed", route.provider, egress.max_sensitivity([sensitivity]))
    started = time.perf_counter()
    provider = embedding_provider(route.provider)
    size = embed_batch_size()
    vectors: List[List[float]] = []
    try:
        for start in range(0, len(inputs), size):
            vectors.extend(provider.embed(model=route.model, inputs=inputs[start : start + size]))
    except Exception:
        _record(route, started, False)
        raise
    _record(route, started, True)
    return vectors


def embed_batch_size() -> int:
    """Inputs per embedding request: large documents become a few requests, not one huge one."""
    return max(1, int(os.getenv("EMBED_BATCH_SIZE") or 64))


def rerank(query: str, documents: List[str], *, sensitivity: Optional[str] = None) -> Optional[List[float]]:
    """Relevance scores aligned with ``documents``, or None when no reranker is configured
    or policy blocks it. Reranking is best-effort; search must work without it."""
    route = rerank_route()
    provider = rerank_provider(route.provider)
    if provider is None or not documents:
        return None
    if not egress.allows(route.provider, sensitivity):
        incr("ai_egress_blocked", labels={"task": "rerank", "action": "skipped"})
        return None
    started = time.perf_counter()
    try:
        scores = provider.rerank(model=route.model, query=query, documents=documents)
    except Exception as exc:
        _record(route, started, False)
        logger.warning("Reranker %s failed; keeping fusion order: %s", route.provider, exc)
        return None
    _record(route, started, True)
    return scores


def transcribe(path: str, *, language: Optional[str] = None, sensitivity: Optional[str] = None) -> List[Dict[str, Any]]:
    route = stt_route()
    if route.provider == "none":
        raise RuntimeError("Speech-to-text is disabled (AI_STT_PROVIDER=none)")
    if is_cloud(route.provider) and not egress.allows(route.provider, sensitivity):
        if route.provider != "local_whisper":
            route = Route(task="stt", provider="local_whisper", model=route.model, source="egress_fallback")
    provider = stt_provider(route.provider)
    started = time.perf_counter()
    try:
        segments = provider.transcribe(model=route.model, path=path, language=language)
    except Exception:
        _record(route, started, False)
        raise
    _record(route, started, True)
    return segments
