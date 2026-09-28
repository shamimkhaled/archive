#!/usr/bin/env python3
"""Create a service/agent API client for /api/v1 and MCP.

Example:
  PYTHONPATH=src python scripts/create_service_client.py \\
    --client-id ask-sonali-agent \\
    --name "Ask Sonali MCP" \\
    --scopes docs:search,docs:meta,docs:status,meetings:list,mcp:invoke \\
    --agent
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from bcp_project.db import get_session
from bcp_project.models import ServiceClient
from bcp_project.service_auth import generate_api_key


async def main() -> None:
    parser = argparse.ArgumentParser(description="Create a BCP service API client")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--scopes",
        default="docs:search,docs:meta,docs:status,meetings:list,mcp:invoke",
        help="Comma-separated scopes",
    )
    parser.add_argument("--rate-limit", type=int, default=120)
    parser.add_argument("--agent", action="store_true", help="Mark as AI agent client")
    parser.add_argument("--created-by", default="admin")
    args = parser.parse_args()

    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    raw, prefix, key_hash = generate_api_key()

    async with get_session() as session:
        existing = await session.execute(
            select(ServiceClient).where(ServiceClient.client_id == args.client_id)
        )
        if existing.scalar_one_or_none() is not None:
            raise SystemExit(f"client_id already exists: {args.client_id}")

        session.add(
            ServiceClient(
                client_id=args.client_id,
                name=args.name,
                key_prefix=prefix,
                key_hash=key_hash,
                scopes=scopes,
                rate_limit_per_minute=args.rate_limit,
                is_active=True,
                is_agent=bool(args.agent),
                created_by=args.created_by,
            )
        )
        await session.commit()

    print("Service client created.")
    print(f"  client_id: {args.client_id}")
    print(f"  scopes:    {', '.join(scopes)}")
    print(f"  api_key:   {raw}")
    print("Store the api_key securely — it will not be shown again.")


if __name__ == "__main__":
    asyncio.run(main())
