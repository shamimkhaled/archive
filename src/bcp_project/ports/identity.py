"""IdentityPort: local password (default), OIDC authorization code + PKCE, and LDAP / Active Directory bind.

The OIDC adapter never trusts anything the browser sends except the authorization code: the ID
token comes from the IdP's token endpoint and is verified against the IdP's JWKS (signature,
issuer, audience, expiry, nonce) before a username is taken from it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("bcp_project.ports.identity")

ROLE_PRIORITY = ("admin", "board_secretary", "board_member", "uploader")


class IdentityError(Exception):
    """Sign-in rejected; `code` is the short reason shown as /login?error=<code>."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass
class ExternalIdentity:
    username: str
    source: str
    email: Optional[str] = None
    groups: List[str] = field(default_factory=list)
    role: Optional[str] = None
    mfa: bool = False


def _truthy(name: str, default: str = "") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


def load_role_map(env_name: str) -> Dict[str, str]:
    """{"CN=Archive-Admins,OU=Groups,DC=bank": "admin", "board-members": "board_member"} (case-insensitive keys)."""
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.error("%s is not valid JSON; no groups will map to roles", env_name)
        return {}
    return {str(k).strip().lower(): str(v).strip() for k, v in data.items() if str(v).strip() in ROLE_PRIORITY}


def map_role(groups: List[str], role_map: Dict[str, str]) -> Optional[str]:
    """Most privileged role granted by any group, or None."""
    granted = {role_map[g.strip().lower()] for g in groups if g and g.strip().lower() in role_map}
    for role in ROLE_PRIORITY:
        if role in granted:
            return role
    return None


def _http_json(url: str, *, data: Optional[bytes] = None, timeout: float = 15) -> Dict[str, Any]:
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    with urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected JSON from {url}")
    return payload


def escape_filter_chars(value: str) -> str:
    """RFC 4515 escaping for a value placed inside an LDAP search filter."""
    out = []
    for ch in value:
        if ch in "\\*()\x00":
            out.append("\\%02x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def pkce_pair() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


class LocalPasswordIdentity:
    def mode(self) -> str:
        return "local"

    def authorization_url(self, **_: Any) -> Optional[str]:
        return None


class OidcIdentityAdapter:
    """Authorization-code flow with PKCE (S256) and nonce against the bank IdP (AD FS, Entra ID, Keycloak)."""

    def __init__(self) -> None:
        self.issuer = (os.getenv("OIDC_ISSUER") or "").rstrip("/")
        self.client_id = os.getenv("OIDC_CLIENT_ID") or ""
        self.client_secret = os.getenv("OIDC_CLIENT_SECRET") or ""
        self.scopes = os.getenv("OIDC_SCOPES", "openid profile email")
        self.username_claim = os.getenv("OIDC_USERNAME_CLAIM", "preferred_username")
        self.groups_claim = os.getenv("OIDC_GROUPS_CLAIM", "groups")
        self.algorithms = [a.strip() for a in os.getenv("OIDC_ALLOWED_ALGS", "RS256,PS256,ES256").split(",") if a.strip()]
        self.leeway = int(os.getenv("OIDC_CLOCK_SKEW_SECONDS", "60"))
        self.role_map = load_role_map("OIDC_ROLE_MAP")
        self._overrides = {
            "authorization_endpoint": os.getenv("OIDC_AUTHORIZE_URL") or "",
            "token_endpoint": os.getenv("OIDC_TOKEN_URL") or "",
            "jwks_uri": os.getenv("OIDC_JWKS_URL") or "",
        }
        self._metadata: Optional[Dict[str, Any]] = None
        self._jwks: Optional[Dict[str, Any]] = None
        self._jwks_fetched = 0.0
        self._lock = threading.Lock()

    def mode(self) -> str:
        return "oidc"

    @property
    def authorize_endpoint(self) -> str:
        return self._endpoint("authorization_endpoint")

    def metadata(self) -> Dict[str, Any]:
        if self._metadata is None:
            meta: Dict[str, Any] = {}
            if self.issuer and not all(self._overrides.values()):
                meta = _http_json(f"{self.issuer}/.well-known/openid-configuration")
                if (meta.get("issuer") or "").rstrip("/") != self.issuer:
                    raise IdentityError("oidc_config", "discovery issuer does not match OIDC_ISSUER")
            meta.update({k: v for k, v in self._overrides.items() if v})
            self._metadata = meta
        return self._metadata

    def _endpoint(self, name: str) -> str:
        try:
            return str(self.metadata().get(name) or "")
        except (URLError, ValueError, IdentityError) as exc:
            logger.warning("OIDC discovery failed: %s", exc)
            return ""

    def authorization_url(self, *, state: str, redirect_uri: str, nonce: str, code_challenge: str) -> Optional[str]:
        endpoint = self.authorize_endpoint
        if not endpoint or not self.client_id:
            return None
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": self.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoint}?{query}"

    def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> Dict[str, Any]:
        endpoint = self._endpoint("token_endpoint")
        if not endpoint or not self.client_id:
            raise IdentityError("oidc_config", "OIDC token endpoint / client_id not configured")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id,
            "code_verifier": code_verifier,
        }
        if self.client_secret:
            form["client_secret"] = self.client_secret
        try:
            payload = _http_json(endpoint, data=urlencode(form).encode("utf-8"))
        except (URLError, ValueError) as exc:
            logger.warning("OIDC token exchange failed: %s", exc)
            raise IdentityError("oidc_token", "token exchange failed") from exc
        if not payload.get("id_token"):
            raise IdentityError("oidc_token", "IdP returned no id_token")
        return payload

    def _signing_keys(self, *, refresh: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            stale = time.monotonic() - self._jwks_fetched > 3600
            if self._jwks is None or refresh or stale:
                uri = self._endpoint("jwks_uri")
                if not uri:
                    raise IdentityError("oidc_config", "no jwks_uri")
                try:
                    self._jwks = _http_json(uri)
                except (URLError, ValueError) as exc:
                    raise IdentityError("oidc_token", f"JWKS fetch failed: {exc}") from exc
                self._jwks_fetched = time.monotonic()
            return list(self._jwks.get("keys") or [])

    def _key_for(self, kid: Optional[str]) -> Dict[str, Any]:
        for refresh in (False, True):
            keys = self._signing_keys(refresh=refresh)
            match = [k for k in keys if kid is None or k.get("kid") == kid]
            if match:
                return match[0]
        raise IdentityError("oidc_token", "ID token signed with an unknown key")

    def verify_id_token(self, id_token: str, *, nonce: str, access_token: Optional[str] = None) -> Dict[str, Any]:
        from jose import JWTError, jwt

        try:
            header = jwt.get_unverified_header(id_token)
        except JWTError as exc:
            raise IdentityError("oidc_token", "malformed ID token") from exc
        if header.get("alg") not in self.algorithms:
            raise IdentityError("oidc_token", f"ID token algorithm {header.get('alg')!r} not allowed")
        key = self._key_for(header.get("kid"))
        try:
            claims = jwt.decode(
                id_token,
                key,
                algorithms=self.algorithms,
                audience=self.client_id,
                issuer=self.issuer or None,
                access_token=access_token,
                options={"leeway": self.leeway, "verify_at_hash": bool(access_token)},
            )
        except JWTError as exc:
            raise IdentityError("oidc_token", f"ID token rejected: {exc}") from exc
        if not nonce or not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise IdentityError("oidc_token", "ID token nonce mismatch")
        azp = claims.get("azp")
        if isinstance(claims.get("aud"), list) and len(claims["aud"]) > 1 and azp != self.client_id:
            raise IdentityError("oidc_token", "ID token azp does not match client")
        return claims

    def identity_from_claims(self, claims: Dict[str, Any]) -> ExternalIdentity:
        username = str(claims.get(self.username_claim) or "").strip()
        if "@" in username and _truthy("OIDC_STRIP_DOMAIN"):
            username = username.split("@", 1)[0]
        if not username:
            raise IdentityError("oidc_map", f"ID token has no {self.username_claim} claim")
        raw_groups = claims.get(self.groups_claim) or []
        groups = [str(g) for g in (raw_groups if isinstance(raw_groups, list) else [raw_groups])]
        amr = claims.get("amr") or []
        return ExternalIdentity(
            username=username[:64],
            source="oidc",
            email=claims.get("email"),
            groups=groups,
            role=map_role(groups, self.role_map),
            mfa=bool({"mfa", "otp", "hwk", "swk", "fido"} & {str(a).lower() for a in amr}),
        )


class LdapIdentityAdapter:
    """Bind-as-service, search the user, bind-as-user, map memberOf groups to a role."""

    def __init__(self) -> None:
        self.url = os.getenv("LDAP_URL", "")
        self.bind_dn = os.getenv("LDAP_BIND_DN", "")
        self.bind_password = os.getenv("LDAP_BIND_PASSWORD", "")
        self.base_dn = os.getenv("LDAP_BASE_DN", "")
        self.user_filter = os.getenv("LDAP_USER_FILTER", "(sAMAccountName={username})")
        self.username_attr = os.getenv("LDAP_USERNAME_ATTR", "sAMAccountName")
        self.start_tls = _truthy("LDAP_START_TLS")
        self.ca_file = os.getenv("LDAP_CA_FILE") or None
        self.timeout = int(os.getenv("LDAP_TIMEOUT_SECONDS", "8"))
        self.role_map = load_role_map("LDAP_ROLE_MAP")

    def _server(self):
        import ssl

        from ldap3 import Server, Tls

        tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=self.ca_file) if (self.url.startswith("ldaps") or self.start_tls) else None
        return Server(self.url, use_ssl=self.url.startswith("ldaps"), tls=tls, connect_timeout=self.timeout)

    def _connect(self, user: str, password: str):
        from ldap3 import Connection

        conn = Connection(self._server(), user=user, password=password, receive_timeout=self.timeout, raise_exceptions=False)
        if self.start_tls and not conn.start_tls():
            raise IdentityError("ldap_unavailable", "StartTLS failed")
        return conn

    def authenticate(self, username: str, password: str) -> ExternalIdentity:
        if not username or not password:
            raise IdentityError("invalid")
        if not self.url or not self.base_dn:
            raise IdentityError("ldap_unavailable", "LDAP_URL / LDAP_BASE_DN not configured")
        service = self._connect(self.bind_dn, self.bind_password)
        try:
            if not service.bind():
                raise IdentityError("ldap_unavailable", "service bind failed")
            service.search(
                self.base_dn,
                self.user_filter.format(username=escape_filter_chars(username)),
                attributes=[self.username_attr, "mail", "memberOf"],
                size_limit=2,
            )
            entries = list(service.entries)
        finally:
            service.unbind()
        if len(entries) != 1:
            raise IdentityError("invalid", "user not found or ambiguous")
        entry = entries[0]
        user_conn = self._connect(entry.entry_dn, password)
        try:
            if not user_conn.bind():
                raise IdentityError("invalid", "bad credentials")
        finally:
            user_conn.unbind()
        attrs = entry.entry_attributes_as_dict
        groups = [str(g) for g in attrs.get("memberOf") or []]
        canonical = str((attrs.get(self.username_attr) or [username])[0]).strip() or username
        mail = attrs.get("mail") or []
        return ExternalIdentity(
            username=canonical[:64],
            source="ldap",
            email=str(mail[0]) if mail else None,
            groups=groups,
            role=map_role(groups, self.role_map),
        )


_port = None
_ldap = None


def oidc_enabled() -> bool:
    return _truthy("OIDC_ENABLED")


def ldap_enabled() -> bool:
    return _truthy("LDAP_ENABLED")


def ldap_local_fallback_allowed(username: str) -> bool:
    """Break-glass: listed local accounts may sign in with their local password when LDAP rejects or is down.

    An allowlist rather than a switch, so an account disabled in the directory cannot fall back
    to a stale local password.
    """
    raw = os.getenv("LDAP_LOCAL_FALLBACK_USERS", "")
    allowed = {u.strip().lower() for u in raw.split(",") if u.strip()}
    return "*" in allowed or (username or "").strip().lower() in allowed


def get_identity_port():
    global _port
    if _port is None:
        _port = OidcIdentityAdapter() if oidc_enabled() else LocalPasswordIdentity()
    return _port


def get_ldap_adapter() -> LdapIdentityAdapter:
    global _ldap
    if _ldap is None:
        _ldap = LdapIdentityAdapter()
    return _ldap


def health_check() -> Dict[str, Any]:
    port = get_identity_port()
    mode = port.mode()
    detail: Dict[str, Any] = {"ok": True, "mode": mode, "oidc_enabled": oidc_enabled(), "ldap_enabled": ldap_enabled()}
    if mode == "oidc":
        detail["client_id_set"] = bool(getattr(port, "client_id", None))
        detail["issuer_set"] = bool(getattr(port, "issuer", None))
        detail["role_map_groups"] = len(getattr(port, "role_map", {}) or {})
        if not detail["client_id_set"] or not detail["issuer_set"]:
            detail["ok"] = False
    if ldap_enabled():
        adapter = get_ldap_adapter()
        detail["ldap_configured"] = bool(adapter.url and adapter.base_dn)
        detail["ldap_role_map_groups"] = len(adapter.role_map)
        try:
            import ldap3  # noqa: F401
        except ImportError:
            detail["ldap_configured"] = False
            detail["ldap_error"] = "ldap3 not installed"
        if not detail["ldap_configured"]:
            detail["ok"] = False
    return detail


def reset_identity_port_for_tests() -> None:
    global _port, _ldap
    _port = None
    _ldap = None
