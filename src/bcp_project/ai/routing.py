"""Resolve which provider and model serve each AI task.

Precedence, highest first:

1. Active ``model_registry`` row for the task (admin switch at /admin/ai, no redeploy)
2. ``AI_TASK_<TASK>_PROVIDER`` / ``AI_TASK_<TASK>_MODEL``
3. ``AI_CHAT_PROVIDER`` / ``AI_CHAT_MODEL`` (or the EMBED / RERANK / STT equivalents)
4. Legacy OpenAI settings (``OPENAI_CHAT_MODEL``, ``OPENAI_EMBEDDING_MODEL``)

Embeddings are deliberately not switchable from the registry: the vector space
must match the collection, so an embedding change goes through
``scripts/reindex_embeddings.py``.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .providers import ALL_PROVIDERS, EMBED_DIMENSIONS, provider_configured

logger = logging.getLogger(__name__)

CHAT_TASKS = ("summary", "minutes", "ask", "board_brief", "metadata")
ROUTABLE_TASKS = tuple(f"chat.{t}" for t in CHAT_TASKS) + ("rerank", "stt")
STT_PROVIDERS = ("local_whisper", "openai", "openrouter", "local", "none")
RERANK_PROVIDERS = ("none", "stub", "local", "openai", "openrouter")

REFRESH_SECONDS = float(os.getenv("AI_REGISTRY_REFRESH_SECONDS", "30"))


@dataclass(frozen=True)
class Route:
    task: str
    provider: str
    model: str
    source: str = "env"
    params: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


_overrides: Dict[str, Route] = {}
_last_refresh = 0.0


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip().strip('"')


def _default_chat_model(provider: str) -> str:
    if provider == "openai":
        from ..config import openai_chat_model

        try:
            return openai_chat_model()
        except RuntimeError:
            return "gpt-4.1-mini"
    if provider == "openrouter":
        return "openai/gpt-4.1-mini"
    if provider == "local":
        return _env("LOCAL_LLM_MODEL") or "qwen2.5:7b-instruct"
    return "stub-chat"


def _default_embed_model(provider: str) -> str:
    if provider == "openai":
        from ..config import openai_embedding_model

        return openai_embedding_model()
    if provider == "openrouter":
        return "openai/text-embedding-3-small"
    if provider == "local":
        return _env("LOCAL_EMBED_MODEL") or "bge-m3"
    return "stub-embed"


def env_chat_route(task: str) -> Route:
    key = task.split(".", 1)[-1].upper()
    provider = (_env(f"AI_TASK_{key}_PROVIDER") or _env("AI_CHAT_PROVIDER") or "openai").lower()
    model = _env(f"AI_TASK_{key}_MODEL") or (
        _env("AI_CHAT_MODEL") if not _env(f"AI_TASK_{key}_PROVIDER") else ""
    ) or _default_chat_model(provider)
    return Route(task=f"chat.{key.lower()}", provider=provider, model=model)


def chat_route(task: str) -> Route:
    name = task if task.startswith("chat.") else f"chat.{task}"
    return _overrides.get(name) or env_chat_route(name)


def embed_route() -> Route:
    provider = (_env("AI_EMBED_PROVIDER") or "openai").lower()
    model = _env("AI_EMBED_MODEL") or _default_embed_model(provider)
    dim = int(_env("AI_EMBED_DIM") or EMBED_DIMENSIONS.get(model.lower(), 0) or EMBED_DIMENSIONS.get(model, 0) or 1536)
    return Route(task="embed", provider=provider, model=model, params={"dim": dim})


def embed_dimension() -> int:
    return int(embed_route().params["dim"])


def rerank_route() -> Route:
    if "rerank" in _overrides:
        return _overrides["rerank"]
    provider = (_env("AI_RERANK_PROVIDER") or "none").lower()
    model = _env("AI_RERANK_MODEL") or ("BAAI/bge-reranker-v2-m3" if provider not in {"none", "stub"} else provider)
    return Route(task="rerank", provider=provider, model=model)


def stt_route() -> Route:
    if "stt" in _overrides:
        return _overrides["stt"]
    raw = (_env("AI_STT_PROVIDER") or "local_whisper").lower()
    provider = "openai" if raw == "openai_whisper" else raw
    if provider == "local_whisper":
        model = _env("AI_STT_MODEL") or _env("WHISPER_MODEL") or "small"
    else:
        model = _env("AI_STT_MODEL") or "whisper-1"
    return Route(task="stt", provider=provider, model=model)


def local_fallback_route(task: str) -> Optional[Route]:
    """Route used when egress policy blocks the primary cloud route, if a local server exists."""
    if not provider_configured("local", "embed" if task == "embed" else "chat"):
        return None
    if task == "embed":
        return Route(task=task, provider="local", model=_env("LOCAL_EMBED_MODEL") or "bge-m3", source="egress_fallback")
    return Route(task=task, provider="local", model=_default_chat_model("local"), source="egress_fallback")


def validate_route(task: str, provider: str, model: str) -> Optional[str]:
    """Error message, or None when the route is acceptable for the admin switch."""
    if task not in ROUTABLE_TASKS:
        return f"Unknown task {task!r}"
    if not model.strip():
        return "Model is required"
    allowed = STT_PROVIDERS if task == "stt" else RERANK_PROVIDERS if task == "rerank" else tuple(sorted(ALL_PROVIDERS))
    if provider not in allowed:
        return f"Provider {provider!r} is not valid for {task}"
    if provider in {"none", "stub", "local_whisper"}:
        return None
    kind = "rerank" if task == "rerank" else "stt" if task == "stt" else "chat"
    if not provider_configured(provider, kind):
        return f"Provider {provider!r} has no credentials or base URL configured"
    return None


def set_overrides(rows: List[Any]) -> None:
    global _overrides, _last_refresh
    fresh: Dict[str, Route] = {}
    for row in rows:
        if row.task in ROUTABLE_TASKS and row.task not in fresh:
            fresh[row.task] = Route(
                task=row.task, provider=row.provider, model=row.model, source="registry", params=dict(row.params or {})
            )
    _overrides = fresh
    _last_refresh = time.monotonic()


def clear_overrides() -> None:
    global _overrides, _last_refresh
    _overrides = {}
    _last_refresh = 0.0


async def refresh_registry(session_factory=None) -> None:
    """Load active overrides from Postgres. Errors keep the previous routes."""
    from sqlalchemy import select

    from ..models import ModelRegistryEntry

    if session_factory is None:
        from ..db import get_session as session_factory
    try:
        async with session_factory() as db:
            rows = (
                await db.execute(
                    select(ModelRegistryEntry)
                    .where(ModelRegistryEntry.is_active.is_(True))
                    .order_by(ModelRegistryEntry.activated_at.desc(), ModelRegistryEntry.id.desc())
                )
            ).scalars().all()
        set_overrides(list(rows))
    except Exception as exc:
        logger.warning("AI model registry refresh failed; keeping previous routes: %s", exc)


async def maybe_refresh_registry(session_factory=None) -> None:
    if time.monotonic() - _last_refresh >= REFRESH_SECONDS:
        await refresh_registry(session_factory)


async def activate_route(db, *, task: str, provider: str, model: str, actor: str, notes: Optional[str] = None):
    """Deactivate the task's current row and insert the new activation. Caller commits."""
    from sqlalchemy import update

    from ..models import ModelRegistryEntry

    now = datetime.utcnow()
    await db.execute(
        update(ModelRegistryEntry)
        .where(ModelRegistryEntry.task == task, ModelRegistryEntry.is_active.is_(True))
        .values(is_active=False, deactivated_at=now)
    )
    row = ModelRegistryEntry(
        task=task, provider=provider, model=model.strip(), is_active=True, notes=notes, activated_by=actor, activated_at=now
    )
    db.add(row)
    await db.flush()
    return row


async def reset_to_env(db, *, task: str) -> int:
    from sqlalchemy import update

    from ..models import ModelRegistryEntry

    result = await db.execute(
        update(ModelRegistryEntry)
        .where(ModelRegistryEntry.task == task, ModelRegistryEntry.is_active.is_(True))
        .values(is_active=False, deactivated_at=datetime.utcnow())
    )
    return int(result.rowcount or 0)


def describe_routes() -> List[Dict[str, Any]]:
    from .egress import requires_local
    from .providers import is_cloud

    rows = []
    for task in CHAT_TASKS:
        route = chat_route(task)
        rows.append({**route.as_dict(), "env": env_chat_route(f"chat.{task}").as_dict(), "cloud": is_cloud(route.provider)})
    for route in (rerank_route(), stt_route()):
        rows.append({**route.as_dict(), "env": None, "cloud": is_cloud(route.provider)})
    emb = embed_route()
    rows.append({**emb.as_dict(), "env": None, "cloud": is_cloud(emb.provider), "locked": True})
    for row in rows:
        row["restricted_needs_local"] = row["cloud"] and requires_local("restricted")
    return rows
