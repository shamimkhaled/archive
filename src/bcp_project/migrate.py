"""Run Alembic migrations from the app, the container entrypoint, or the CLI.

``python -m bcp_project.migrate`` upgrades to head. On Postgres an advisory
lock makes concurrent starts (several Railway replicas) run migrations once.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("bcp_project.migrate")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
MIGRATION_LOCK_ID = 7_391_204_118


def auto_migrate_enabled() -> bool:
    return (os.getenv("AUTO_MIGRATE") or "1").strip().lower() not in {"0", "false", "no", "off"}


def alembic_config() -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return cfg


def upgrade_connection(connection: Connection, revision: str = "head") -> None:
    """Upgrade using an open sync connection (works with run_sync on async engines)."""
    is_pg = connection.dialect.name == "postgresql"
    if is_pg:
        connection.execute(text("SELECT pg_advisory_lock(:id)"), {"id": MIGRATION_LOCK_ID})
    try:
        cfg = alembic_config()
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, revision)
    finally:
        if is_pg:
            connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": MIGRATION_LOCK_ID})


async def upgrade_head(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.run_sync(upgrade_connection)
        await conn.commit()
    logger.info("Database schema is at Alembic head")


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    from .db import engine

    async def _go() -> None:
        await upgrade_head(engine)
        await engine.dispose()

    asyncio.run(_go())


if __name__ == "__main__":
    main()
