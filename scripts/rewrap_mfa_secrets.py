#!/usr/bin/env python3
"""Re-encrypt every stored TOTP secret under the current MFA key after a key rotation.

Run with the new MFA_ENCRYPTION_KEY (or JWT_SECRET_KEY) set and the old one in
MFA_ENCRYPTION_KEY_PREVIOUS (or JWT_SECRET_KEY_PREVIOUS). Afterwards the previous key can be removed.

  PYTHONPATH=src python scripts/rewrap_mfa_secrets.py            # dry run: counts only
  PYTHONPATH=src python scripts/rewrap_mfa_secrets.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bcp_project.config import load_environment

load_environment()

from sqlalchemy import select

from bcp_project.db import get_session
from bcp_project.mfa import rewrap_secret
from bcp_project.models import UserMfa


async def main(apply: bool) -> int:
    rewrapped, unreadable = 0, []
    async with get_session() as db:
        for row in (await db.execute(select(UserMfa))).scalars():
            token = rewrap_secret(row.secret_encrypted)
            if token is None:
                unreadable.append(row.username)
                continue
            row.secret_encrypted = token
            rewrapped += 1
        if apply:
            await db.commit()
        else:
            await db.rollback()
    print(f"{'re-encrypted' if apply else 'would re-encrypt'} {rewrapped} secret(s)")
    if unreadable:
        print(f"no configured key opens {len(unreadable)} enrolment(s): {', '.join(unreadable)} (reset their MFA)")
    return 1 if unreadable else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args().apply)))
