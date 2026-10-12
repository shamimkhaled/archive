"""Which document sensitivities may be sent to a cloud AI provider.

``AI_EGRESS``:

``cloud_allowed``          public, internal, confidential may use cloud models
``restricted_local_only``  confidential and restricted stay on bank hardware
``internal_only``          nothing leaves the bank

``restricted`` never goes to a cloud provider under any setting.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

from ..models import Sensitivity, parse_sensitivity
from .providers import is_cloud

POLICIES = ("cloud_allowed", "restricted_local_only", "internal_only")

_RANK = {
    Sensitivity.public.value: 0,
    Sensitivity.internal.value: 1,
    Sensitivity.confidential.value: 2,
    Sensitivity.restricted.value: 3,
}

_LOCAL_ONLY = {
    "cloud_allowed": {Sensitivity.restricted.value},
    "restricted_local_only": {Sensitivity.confidential.value, Sensitivity.restricted.value},
    "internal_only": set(_RANK),
}


class EgressBlocked(RuntimeError):
    """Raised when policy forbids sending this content to the configured provider."""

    def __init__(self, task: str, provider: str, sensitivity: str):
        self.task = task
        self.provider = provider
        self.sensitivity = sensitivity
        super().__init__(
            f"AI egress policy {egress_policy()!r} forbids sending {sensitivity} content "
            f"to cloud provider {provider!r} for {task}; configure a local provider"
        )


def egress_policy() -> str:
    value = (os.getenv("AI_EGRESS") or "cloud_allowed").strip().lower()
    return value if value in POLICIES else "restricted_local_only"


def max_sensitivity(values: Iterable[Optional[str]]) -> str:
    best = Sensitivity.public.value
    seen = False
    for value in values:
        level = parse_sensitivity(value)
        seen = True
        if _RANK[level] > _RANK[best]:
            best = level
    return best if seen else Sensitivity.internal.value


def requires_local(sensitivity: Optional[str]) -> bool:
    return parse_sensitivity(sensitivity) in _LOCAL_ONLY[egress_policy()]


def allows(provider: str, sensitivity: Optional[str]) -> bool:
    return not (is_cloud(provider) and requires_local(sensitivity))
