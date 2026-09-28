"""Adapter registry with health probes and circuit breakers."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .circuit import CircuitBreaker

logger = logging.getLogger("bcp_project.ports.registry")


@dataclass
class AdapterRegistration:
    name: str
    kind: str
    factory: Callable[[], Any]
    health: Optional[Callable[[], Dict[str, Any]]] = None
    circuit: CircuitBreaker = field(default_factory=lambda: CircuitBreaker("unnamed"))
    description: str = ""


class AdapterRegistry:
    def __init__(self) -> None:
        self._items: Dict[str, AdapterRegistration] = {}

    def register(self, reg: AdapterRegistration) -> None:
        if not reg.circuit.name or reg.circuit.name == "unnamed":
            reg.circuit = CircuitBreaker(reg.name)
        self._items[reg.name] = reg
        logger.info("Registered adapter name=%s kind=%s", reg.name, reg.kind)

    def get(self, name: str) -> AdapterRegistration:
        if name not in self._items:
            raise KeyError(f"Unknown adapter: {name}")
        return self._items[name]

    def names(self) -> List[str]:
        return sorted(self._items.keys())

    def health_all(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {"adapters": {}, "ok": True}
        for name, reg in sorted(self._items.items()):
            entry: Dict[str, Any] = {
                "kind": reg.kind,
                "description": reg.description,
                "circuit": reg.circuit.snapshot(),
            }
            try:
                if reg.health is not None:
                    entry["health"] = reg.health()
                else:
                    # Touch factory construct for existence.
                    reg.factory()
                    entry["health"] = {"ok": True}
            except Exception as exc:
                entry["health"] = {"ok": False, "error": str(exc)[:200]}
                report["ok"] = False
            if entry["circuit"].get("open"):
                report["ok"] = False
            report["adapters"][name] = entry
        return report


_registry: Optional[AdapterRegistry] = None


def get_registry() -> AdapterRegistry:
    global _registry
    if _registry is None:
        _registry = AdapterRegistry()
        _bootstrap_registry(_registry)
    return _registry


def reset_registry_for_tests() -> None:
    global _registry
    _registry = None


def _bootstrap_registry(registry: AdapterRegistry) -> None:
    from . import adapters as core
    from . import customer as customer_mod
    from . import document_source as doc_src
    from . import events as events_mod
    from . import identity as identity_mod
    from . import loan_hr as loan_hr_mod

    registry.register(
        AdapterRegistration(
            name="llm",
            kind="llm",
            factory=core.get_llm_client,
            description="OpenAI/OpenRouter chat + embeddings",
        )
    )
    registry.register(
        AdapterRegistration(
            name="vector_store",
            kind="vector",
            factory=core.get_vector_store,
            description="Qdrant vector index",
        )
    )
    registry.register(
        AdapterRegistration(
            name="object_storage",
            kind="storage",
            factory=core.get_object_storage,
            health=lambda: {"ok": True, "backend": core.get_object_storage().storage_backend_name()},
            description="S3/local object storage",
        )
    )
    registry.register(
        AdapterRegistration(
            name="email",
            kind="email",
            factory=core.get_email_client,
            description="Resend meeting email",
        )
    )
    registry.register(
        AdapterRegistration(
            name="document_source",
            kind="document_source",
            factory=doc_src.get_document_source,
            health=doc_src.health_check,
            description="Inbox / SFTP document pull source",
        )
    )
    registry.register(
        AdapterRegistration(
            name="customer",
            kind="customer",
            factory=customer_mod.get_customer_port,
            health=customer_mod.health_check,
            description="Read-minimized customer summary (CBS facade)",
        )
    )
    registry.register(
        AdapterRegistration(
            name="loan",
            kind="loan",
            factory=loan_hr_mod.get_loan_port,
            health=loan_hr_mod.loan_health,
            description="Read-minimized loan summary facade",
        )
    )
    registry.register(
        AdapterRegistration(
            name="hr",
            kind="hr",
            factory=loan_hr_mod.get_hr_port,
            health=loan_hr_mod.hr_health,
            description="Read-minimized HR employee facade",
        )
    )
    registry.register(
        AdapterRegistration(
            name="identity",
            kind="identity",
            factory=identity_mod.get_identity_port,
            health=identity_mod.health_check,
            description="Local password and optional OIDC",
        )
    )
    registry.register(
        AdapterRegistration(
            name="events",
            kind="events",
            factory=events_mod.get_event_publisher,
            health=events_mod.health_check,
            description="Outbound webhooks / event fan-out",
        )
    )
