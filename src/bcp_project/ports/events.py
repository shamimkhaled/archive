"""Outbound event publisher — signed webhooks for bank MQ/ESB bridges."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from ..correlation import get_correlation_id
from ..observability import incr

logger = logging.getLogger("bcp_project.ports.events")


class NullEventPublisher:
    def publish(self, event_type: str, payload: Dict[str, Any]) -> bool:
        logger.debug("Null event publisher dropped %s", event_type)
        return True


class WebhookEventPublisher:
    """POST JSON events to comma-separated WEBHOOK_URLS with HMAC signature."""

    def __init__(self) -> None:
        raw = os.getenv("WEBHOOK_URLS") or ""
        self.urls: List[str] = [u.strip() for u in raw.split(",") if u.strip()]
        self.secret = (os.getenv("WEBHOOK_SECRET") or "").encode("utf-8")
        self.timeout = float(os.getenv("WEBHOOK_TIMEOUT_SECONDS", "8"))

    def publish(self, event_type: str, payload: Dict[str, Any]) -> bool:
        if not self.urls:
            return True
        envelope = {
            "event_type": event_type,
            "occurred_at": datetime.utcnow().isoformat() + "Z",
            "correlation_id": get_correlation_id(),
            "payload": payload,
        }
        body = json.dumps(envelope, ensure_ascii=False, default=str).encode("utf-8")
        signature = ""
        if self.secret:
            signature = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        ok_all = True
        for url in self.urls:
            headers = {
                "Content-Type": "application/json",
                "X-BCP-Event": event_type,
                "User-Agent": "bcp-webhook/1.0",
            }
            if signature:
                headers["X-BCP-Signature"] = f"sha256={signature}"
            req = Request(url, data=body, headers=headers, method="POST")
            try:
                with urlopen(req, timeout=self.timeout) as resp:
                    if resp.status >= 300:
                        ok_all = False
                        logger.warning("Webhook %s returned %s", url, resp.status)
                    else:
                        incr("events.webhook.ok")
            except URLError as exc:
                ok_all = False
                incr("events.webhook.error")
                logger.warning("Webhook publish failed url=%s err=%s", url, exc)
        return ok_all


_publisher = None


def get_event_publisher():
    global _publisher
    if _publisher is None:
        urls = (os.getenv("WEBHOOK_URLS") or "").strip()
        _publisher = WebhookEventPublisher() if urls else NullEventPublisher()
    return _publisher


def health_check() -> Dict[str, Any]:
    urls = [u.strip() for u in (os.getenv("WEBHOOK_URLS") or "").split(",") if u.strip()]
    return {
        "ok": True,
        "mode": "webhook" if urls else "null",
        "endpoints": len(urls),
        "signing": bool(os.getenv("WEBHOOK_SECRET")),
    }


def publish_event(event_type: str, payload: Dict[str, Any]) -> bool:
    from .registry import get_registry

    reg = get_registry().get("events")
    try:
        return reg.circuit.call(lambda: get_event_publisher().publish(event_type, payload))
    except Exception:
        logger.exception("Event publish failed for %s", event_type)
        return False


def reset_event_publisher_for_tests() -> None:
    global _publisher
    _publisher = None
