"""CI check against a migrated Postgres: audit_logs rejects edits and deletes, accepts exactly one seal.

    DATABASE_URL=postgresql+asyncpg://... PYTHONPATH=src python scripts/check_audit_append_only.py
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from bcp_project.db import engine


async def expect_rejected(conn, sql: str, params: dict, label: str) -> bool:
    try:
        async with conn.begin_nested():
            await conn.execute(text(sql), params)
    except DBAPIError:
        print(f"ok   {label} rejected")
        return True
    print(f"FAIL {label} was allowed")
    return False


async def main() -> int:
    async with engine.connect() as conn:
        trans = await conn.begin()
        row_id = (
            await conn.execute(
                text(
                    "INSERT INTO audit_logs (username, action, resource_type, created_at) "
                    "VALUES ('ci', 'ci_check', 'ci', now()) RETURNING id"
                )
            )
        ).scalar_one()
        params = {"id": row_id}
        checks = [
            await expect_rejected(conn, "UPDATE audit_logs SET action = 'tampered' WHERE id = :id", params, "content edit"),
            await expect_rejected(conn, "DELETE FROM audit_logs WHERE id = :id", params, "delete"),
            await expect_rejected(conn, "TRUNCATE audit_logs", {}, "truncate"),
        ]
        await conn.execute(
            text("UPDATE audit_logs SET prev_hash = :h, row_hash = :h WHERE id = :id"), {**params, "h": "0" * 64}
        )
        print("ok   first seal accepted")
        checks.append(
            await expect_rejected(
                conn, "UPDATE audit_logs SET row_hash = :h WHERE id = :id", {**params, "h": "1" * 64}, "re-seal"
            )
        )
        await trans.rollback()
    await engine.dispose()
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
