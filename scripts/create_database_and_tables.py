import asyncio
import os
import sys
from urllib.parse import urlparse
from pathlib import Path

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from bcp_project.db import engine
from bcp_project.models import Base

DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL is None:
    raise SystemExit('DATABASE_URL must be set')

parsed = urlparse(DATABASE_URL)
if parsed.scheme not in ('postgresql+asyncpg', 'postgresql'):
    raise SystemExit('DATABASE_URL must use postgresql+asyncpg or postgresql scheme')

dbname = parsed.path.lstrip('/')
admin_db = 'postgres'
user = parsed.username or 'postgres'
password = parsed.password or ''
host = parsed.hostname or 'localhost'
port = parsed.port or 5432

async def create_database():
    conn = psycopg.connect(host=host, port=port, user=user, password=password, dbname=admin_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql.SQL('SELECT 1 FROM pg_database WHERE datname = %s'), [dbname])
        exists = cur.fetchone() is not None
        if not exists:
            cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(dbname)))
            print(f'Created database {dbname}')
        else:
            print(f'Database {dbname} already exists')
    conn.close()

async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print('Created tables successfully')

if __name__ == '__main__':
    print('DATABASE_URL:', DATABASE_URL)
    asyncio.run(create_database())
    asyncio.run(create_tables())
