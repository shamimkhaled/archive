"""Minimal stdio MCP server (JSON-RPC 2.0) for read-only archive tools.

Environment:
  BCP_MCP_API_KEY     — service client API key (required)
  BCP_MCP_ON_BEHALF   — username for grant-scoped access (recommended)
  DATABASE_URL        — Postgres (same as app)

Run:
  PYTHONPATH=src python -m bcp_project.mcp.server
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

from ..config import load_environment
from ..db import get_session
from ..policy import Principal
from ..service_auth import (
    lookup_service_client,
    merge_service_scopes,
    resolve_on_behalf_user,
    scopes_from_client,
)
from .tools import TOOL_DEFINITIONS, invoke_tool

logger = logging.getLogger("bcp_project.mcp.server")

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "sonali-bank-archive", "version": "0.1.0"}


def _write(message: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _error(req_id: Any, code: int, message: str) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


async def _build_principal() -> Principal:
    api_key = (os.getenv("BCP_MCP_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("BCP_MCP_API_KEY is required for the MCP server")
    on_behalf = (os.getenv("BCP_MCP_ON_BEHALF") or "").strip() or None
    async with get_session() as db:
        client = await lookup_service_client(db, api_key)
        if client is None:
            raise RuntimeError("Invalid BCP_MCP_API_KEY")
        user = await resolve_on_behalf_user(db, on_behalf) if on_behalf else None
        scopes = merge_service_scopes(scopes_from_client(client), user)
        return Principal(
            actor_id=client.client_id,
            actor_type="agent" if client.is_agent else "service",
            scopes=scopes,
            role=user.role if user else None,
            user=user,
            client_name=client.name,
        )


async def _handle(req: Dict[str, Any], principal: Principal) -> None:
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if method == "initialize":
        _write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                },
            }
        )
        return

    if method == "notifications/initialized":
        return

    if method == "tools/list":
        _write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOL_DEFINITIONS},
            }
        )
        return

    if method == "tools/call":
        name = (params.get("name") or "").strip()
        arguments = params.get("arguments") or {}
        try:
            async with get_session() as db:
                result = await invoke_tool(
                    name,
                    arguments if isinstance(arguments, dict) else {},
                    db=db,
                    principal=principal,
                )
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result, ensure_ascii=False, default=str),
                            }
                        ],
                        "isError": False,
                    },
                }
            )
        except Exception as exc:
            logger.exception("tools/call failed")
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                }
            )
        return

    if method == "ping":
        _write({"jsonrpc": "2.0", "id": req_id, "result": {}})
        return

    if req_id is not None:
        _error(req_id, -32601, f"Method not found: {method}")


async def run_stdio() -> None:
    load_environment()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    principal = await _build_principal()
    logger.info(
        "MCP stdio server ready client=%s actor_type=%s on_behalf=%s",
        principal.actor_id,
        principal.actor_type,
        principal.username_for_grants,
    )
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _error(None, -32700, "Parse error")
            continue
        if not isinstance(req, dict):
            continue
        await _handle(req, principal)


def main() -> None:
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
