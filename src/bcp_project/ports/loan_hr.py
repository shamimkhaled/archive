"""Loan and HR read-minimized facade stubs (bank systems unchanged)."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("bcp_project.ports.loan_hr")

LOAN_ALLOWED = ("loan_ref", "product", "status", "branch_code", "currency")
HR_ALLOWED = ("employee_id", "display_name", "department", "status", "branch_code")


def _min(payload: Dict[str, Any], allowed: tuple[str, ...]) -> Dict[str, Any]:
    return {k: payload[k] for k in allowed if k in payload and payload[k] is not None}


class MockLoanAdapter:
    def get_loan_summary(self, loan_ref: str) -> Optional[Dict[str, Any]]:
        ref = (loan_ref or "").strip()
        if not ref:
            return None
        return _min(
            {
                "loan_ref": ref,
                "product": "SME Working Capital",
                "status": "active",
                "branch_code": os.getenv("MOCK_LOAN_BRANCH", "DHK-01"),
                "currency": "BDT",
                "principal": 1_000_000,  # stripped
            },
            LOAN_ALLOWED,
        )


class MockHrAdapter:
    def get_employee_summary(self, employee_id: str) -> Optional[Dict[str, Any]]:
        emp = (employee_id or "").strip()
        if not emp:
            return None
        return _min(
            {
                "employee_id": emp,
                "display_name": f"Officer {emp}",
                "department": "ICT",
                "status": "active",
                "branch_code": "HO",
                "salary": 0,  # stripped
            },
            HR_ALLOWED,
        )


_loan = None
_hr = None


def get_loan_port():
    global _loan
    if _loan is None:
        _loan = MockLoanAdapter()
    return _loan


def get_hr_port():
    global _hr
    if _hr is None:
        _hr = MockHrAdapter()
    return _hr


def loan_health() -> Dict[str, Any]:
    try:
        sample = get_loan_port().get_loan_summary("LN-DEMO")
        return {"ok": True, "mode": "mock", "sample_ok": sample is not None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def hr_health() -> Dict[str, Any]:
    try:
        sample = get_hr_port().get_employee_summary("EMP-DEMO")
        return {"ok": True, "mode": "mock", "sample_ok": sample is not None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def reset_loan_hr_for_tests() -> None:
    global _loan, _hr
    _loan = None
    _hr = None
