import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from sqlalchemy import select

from bcp_project.auth import get_password_hash
from bcp_project.config import load_environment
from bcp_project.db import get_session
from bcp_project.models import Role, User

load_environment()


async def create_admin(username: str, password: str, update: bool) -> None:
    async with get_session() as session:
        result = await session.execute(select(User).where(User.username == username))
        existing = result.scalar_one_or_none()

        if existing is not None and not update:
            raise SystemExit(
                f"User '{username}' already exists. Re-run with --update to reset their password/role."
            )

        if existing is not None:
            existing.hashed_password = get_password_hash(password)
            existing.role = Role.admin
            existing.is_active = True
            await session.commit()
            print(f"Updated existing user '{username}' to an active admin.")
            return

        session.add(User(
            username=username,
            hashed_password=get_password_hash(password),
            role=Role.admin,
            is_active=True,
        ))
        await session.commit()
        print(f"Created admin user '{username}'.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Bootstrap or reset an admin user directly in the database.')
    parser.add_argument('--username', required=True, help='Admin username to create or update.')
    parser.add_argument('--password', help='Admin password. Omit to be prompted securely.')
    parser.add_argument('--update', action='store_true', help='Reset the password/role if the user already exists.')
    args = parser.parse_args()

    password = args.password or getpass.getpass('Admin password: ')
    if not password:
        raise SystemExit('Password must not be empty.')

    asyncio.run(create_admin(args.username, password, args.update))
