"""CI check against a migrated Postgres: audit_logs rejects edits and deletes, accepts exactly one seal.

search_activity gets the same treatment: one audit link is accepted, edits and deletes are not,
except a delete inside a transaction that set bcp.search_log_purge (the retention purge).

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

        activity_id = (
            await conn.execute(
                text(
                    "INSERT INTO search_activity (username, channel, query, created_at) "
                    "VALUES ('ci', 'web_search', 'ci check', now()) RETURNING id"
                )
            )
        ).scalar_one()
        params = {"id": activity_id}
        checks += [
            await expect_rejected(conn, "UPDATE search_activity SET query = 'tampered' WHERE id = :id", params, "search query edit"),
            await expect_rejected(conn, "DELETE FROM search_activity WHERE id = :id", params, "search delete"),
            await expect_rejected(conn, "TRUNCATE search_activity", {}, "search truncate"),
        ]
        await conn.execute(text("UPDATE search_activity SET audit_log_id = :a WHERE id = :id"), {**params, "a": row_id})
        print("ok   search audit link accepted")
        checks.append(
            await expect_rejected(
                conn, "UPDATE search_activity SET audit_log_id = 0 WHERE id = :id", params, "search re-link"
            )
        )
        async with conn.begin_nested():
            await conn.execute(text("SET LOCAL bcp.search_log_purge = 'on'"))
            await conn.execute(text("DELETE FROM search_activity WHERE id = :id"), params)
        print("ok   retention purge delete accepted")
        await trans.rollback()
    await engine.dispose()
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
