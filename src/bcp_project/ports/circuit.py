"""Simple circuit breaker for outbound bank adapters."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable, Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass
class CircuitState:
    failures: int = 0
    opened_at: Optional[float] = None
    last_error: Optional[str] = None


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    reset_timeout_seconds: float = 30.0
    _state: CircuitState = field(default_factory=CircuitState)
    _lock: Lock = field(default_factory=Lock)

    def allow(self) -> bool:
        with self._lock:
            if self._state.opened_at is None:
                return True
            if time.monotonic() - self._state.opened_at >= self.reset_timeout_seconds:
                # Half-open: allow one probe.
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._state.failures = 0
            self._state.opened_at = None
            self._state.last_error = None

    def record_failure(self, error: str) -> None:
        with self._lock:
            self._state.failures += 1
            self._state.last_error = error[:240]
            if self._state.failures >= self.failure_threshold:
                self._state.opened_at = time.monotonic()

    @property
    def is_open(self) -> bool:
        return not self.allow()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "failures": self._state.failures,
                "open": self._state.opened_at is not None and not self.allow(),
                "last_error": self._state.last_error,
            }

    def call(self, fn: Callable[[], T]) -> T:
        if not self.allow():
            raise RuntimeError(f"Circuit open for adapter '{self.name}'")
        try:
            result = fn()
            self.record_success()
            return result
        except Exception as exc:
            self.record_failure(str(exc))
            raise
