"""CustomerPort — read-minimized CBS/CIF facade (never full ledger)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("bcp_project.ports.customer")

# Hard allowlist — AI/tools never receive unrestricted CBS fields.
ALLOWED_FIELDS = (
    "customer_ref",
    "display_name",
    "branch_code",
    "segment",
    "status",
    "relationship_manager",
)


def _minimize(payload: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in ALLOWED_FIELDS:
        if key in payload and payload[key] is not None:
            out[key] = payload[key]
    return out


class MockCustomerAdapter:
    """Deterministic demo data for labs without CBS connectivity."""

    def get_customer_summary(self, customer_ref: str) -> Optional[Dict[str, Any]]:
        ref = (customer_ref or "").strip()
        if not ref:
            return None
        return _minimize(
            {
                "customer_ref": ref,
                "display_name": f"Customer {ref}",
                "branch_code": os.getenv("MOCK_CUSTOMER_BRANCH", "DHK-01"),
                "segment": "retail",
                "status": "active",
                "relationship_manager": "desk-demo",
            }
        )


class HttpCustomerAdapter:
    """GET {CUSTOMER_API_BASE_URL}/{ref} with bearer/token auth; field-minimized."""

    def __init__(self) -> None:
        self.base_url = (os.getenv("CUSTOMER_API_BASE_URL") or "").rstrip("/")
        self.token = os.getenv("CUSTOMER_API_TOKEN") or ""
        self.timeout = float(os.getenv("CUSTOMER_API_TIMEOUT_SECONDS", "8"))

    def get_customer_summary(self, customer_ref: str) -> Optional[Dict[str, Any]]:
        ref = (customer_ref or "").strip()
        if not ref or not self.base_url:
            return None
        url = f"{self.base_url}/{ref}"
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = Request(url, headers=headers, method="GET")
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            if not isinstance(raw, dict):
                return None
            return _minimize(raw)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            logger.warning("Customer API HTTP error %s for %s", exc.code, ref)
            raise
        except URLError as exc:
            logger.warning("Customer API unreachable: %s", exc)
            raise


_port = None


def get_customer_port():
    global _port
    if _port is None:
        mode = (os.getenv("CUSTOMER_PORT") or "mock").strip().lower()
        if mode == "http":
            _port = HttpCustomerAdapter()
        else:
            _port = MockCustomerAdapter()
    return _port


def health_check() -> Dict[str, Any]:
    mode = (os.getenv("CUSTOMER_PORT") or "mock").strip().lower()
    try:
        port = get_customer_port()
        sample = port.get_customer_summary(os.getenv("CUSTOMER_HEALTH_REF", "DEMO-001"))
        return {"ok": True, "mode": mode, "sample_ok": sample is not None}
    except Exception as exc:
        return {"ok": False, "mode": mode, "error": str(exc)[:200]}


def reset_customer_port_for_tests() -> None:
    global _port
    _port = None
