import argparse
import asyncio
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description='Create SQLAlchemy tables in a PostgreSQL database.')
parser.add_argument('--database-url', help='Database URL to use for table creation.')
args = parser.parse_args()

if args.database_url:
    os.environ['DATABASE_URL'] = args.database_url

if 'DATABASE_URL' not in os.environ or not os.environ['DATABASE_URL']:
    raise SystemExit('DATABASE_URL must be set either via environment or --database-url')

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from bcp_project.db import engine
from bcp_project.models import Base

async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

if __name__ == '__main__':
    print('DATABASE_URL:', os.environ['DATABASE_URL'])
    asyncio.run(create_tables())
    print('Tables created successfully')
