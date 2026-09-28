"""IdentityPort — local password (default) + OIDC scaffolding."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("bcp_project.ports.identity")


class LocalPasswordIdentity:
    def mode(self) -> str:
        return "local"

    def authorization_url(self, *, state: str, redirect_uri: str) -> Optional[str]:
        return None

    def exchange_code(self, *, code: str, redirect_uri: str) -> Optional[Dict[str, Any]]:
        return None


class OidcIdentityAdapter:
    """Authorization-code OIDC scaffolding (bank IdP / AD FS / Entra).

    Enable with OIDC_ENABLED=1 and OIDC_* env vars. Does not replace local login
    until the bank IdP is wired and tested.
    """

    def __init__(self) -> None:
        self.issuer = (os.getenv("OIDC_ISSUER") or "").rstrip("/")
        self.client_id = os.getenv("OIDC_CLIENT_ID") or ""
        self.client_secret = os.getenv("OIDC_CLIENT_SECRET") or ""
        self.authorize_endpoint = os.getenv("OIDC_AUTHORIZE_URL") or (
            f"{self.issuer}/authorize" if self.issuer else ""
        )
        self.token_endpoint = os.getenv("OIDC_TOKEN_URL") or (
            f"{self.issuer}/oauth/token" if self.issuer else ""
        )
        self.scopes = os.getenv("OIDC_SCOPES", "openid profile email")

    def mode(self) -> str:
        return "oidc"

    def authorization_url(self, *, state: str, redirect_uri: str) -> Optional[str]:
        if not self.authorize_endpoint or not self.client_id:
            return None
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": self.scopes,
                "state": state,
            }
        )
        return f"{self.authorize_endpoint}?{query}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> Optional[Dict[str, Any]]:
        if not self.token_endpoint or not self.client_id:
            raise RuntimeError("OIDC token endpoint / client_id not configured")
        body = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode("utf-8")
        req = Request(
            self.token_endpoint,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if not isinstance(payload, dict):
                return None
            return {
                "access_token": payload.get("access_token"),
                "id_token": payload.get("id_token"),
                "token_type": payload.get("token_type"),
                "expires_in": payload.get("expires_in"),
                "raw_keys": sorted(payload.keys()),
            }
        except URLError as exc:
            logger.warning("OIDC token exchange failed: %s", exc)
            raise


_port = None


def oidc_enabled() -> bool:
    return (os.getenv("OIDC_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}


def get_identity_port():
    global _port
    if _port is None:
        _port = OidcIdentityAdapter() if oidc_enabled() else LocalPasswordIdentity()
    return _port


def health_check() -> Dict[str, Any]:
    port = get_identity_port()
    mode = port.mode()
    ok = True
    detail: Dict[str, Any] = {"ok": ok, "mode": mode, "oidc_enabled": oidc_enabled()}
    if mode == "oidc":
        detail["authorize_configured"] = bool(getattr(port, "authorize_endpoint", None))
        detail["client_id_set"] = bool(getattr(port, "client_id", None))
        if not detail["authorize_configured"] or not detail["client_id_set"]:
            detail["ok"] = False
    return detail


def reset_identity_port_for_tests() -> None:
    global _port
    _port = None
