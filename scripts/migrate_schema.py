import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from sqlalchemy import text

from bcp_project.db import engine
from bcp_project.models import Base

NEW_ROLE_VALUES = ["admin", "board_secretary", "uploader", "board_member"]
# Maps roles from the pre-migration schema onto their closest new equivalent.
LEGACY_ROLE_MAP = {"admin": "admin", "user": "uploader", "board": "board_secretary"}


async def migrate() -> None:
    async with engine.begin() as conn:
        # Idempotently create any tables that don't exist yet.
        await conn.run_sync(Base.metadata.create_all)

        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255)"))
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS notifications_enabled "
                "BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )
        print("users.email / notifications_enabled columns present.")

        await conn.execute(text("ALTER TABLE board_meetings ADD COLUMN IF NOT EXISTS attendance_open BOOLEAN NOT NULL DEFAULT FALSE"))
        await conn.execute(text("ALTER TABLE board_meetings ADD COLUMN IF NOT EXISTS attendance_opened_at TIMESTAMP"))
        await conn.execute(text("ALTER TABLE board_meetings ADD COLUMN IF NOT EXISTS attendance_closed_at TIMESTAMP"))
        await conn.execute(
            text(
                "ALTER TABLE board_meetings ADD COLUMN IF NOT EXISTS notifications_enabled "
                "BOOLEAN NOT NULL DEFAULT TRUE"
            )
        )
        print("board_meetings attendance / notifications columns present.")

        await conn.execute(text("ALTER TABLE meeting_invitations ADD COLUMN IF NOT EXISTS invitation_email_sent_at TIMESTAMP"))
        await conn.execute(text("ALTER TABLE meeting_invitations ADD COLUMN IF NOT EXISTS reminder_48h_sent_at TIMESTAMP"))
        await conn.execute(text("ALTER TABLE meeting_invitations ADD COLUMN IF NOT EXISTS reminder_24h_sent_at TIMESTAMP"))
        await conn.execute(text("ALTER TABLE meeting_invitations DROP COLUMN IF EXISTS reminder_sent"))
        print("meeting_invitations reminder columns present.")

        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS notification_events (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(64) NOT NULL,
                    kind VARCHAR(64) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    body TEXT DEFAULT '',
                    meeting_id INTEGER REFERENCES board_meetings(id),
                    created_at TIMESTAMP,
                    read_at TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_notification_events_username ON notification_events (username)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_notification_events_meeting_id ON notification_events (meeting_id)")
        )
        print("notification_events table present.")

        await conn.execute(
            text("ALTER TABLE documents ADD COLUMN IF NOT EXISTS summary_json JSON")
        )
        print("documents.summary_json column present.")

        await conn.execute(
            text(
                """
                DO $$ BEGIN
                    CREATE TYPE accessmode AS ENUM ('view_only', 'download');
                EXCEPTION WHEN duplicate_object THEN null; END $$;
                """
            )
        )
        await conn.execute(
            text(
                """
                DO $$ BEGIN
                    CREATE TYPE accessrequeststatus AS ENUM ('pending', 'approved', 'denied', 'revoked');
                EXCEPTION WHEN duplicate_object THEN null; END $$;
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS document_access_requests (
                    id SERIAL PRIMARY KEY,
                    doc_id VARCHAR(128) NOT NULL,
                    requester_username VARCHAR(64) NOT NULL,
                    purpose TEXT NOT NULL,
                    requested_mode accessmode NOT NULL,
                    status accessrequeststatus NOT NULL DEFAULT 'pending',
                    reviewed_by VARCHAR(64),
                    review_note TEXT,
                    created_at TIMESTAMP,
                    reviewed_at TIMESTAMP,
                    expires_at TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_document_access_requests_doc_id ON document_access_requests (doc_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_document_access_requests_requester_username ON document_access_requests (requester_username)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_document_access_requests_status ON document_access_requests (status)"
            )
        )
        print("document_access_requests table present.")

        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(64) NOT NULL,
                    action VARCHAR(64) NOT NULL,
                    resource_type VARCHAR(64) NOT NULL,
                    resource_id VARCHAR(128),
                    detail TEXT,
                    ip_address VARCHAR(64),
                    created_at TIMESTAMP
                )
                """
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_logs_username ON audit_logs (username)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_logs_action ON audit_logs (action)"))
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_audit_logs_resource_id ON audit_logs (resource_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at)")
        )
        print("audit_logs table present.")

        result = await conn.execute(text(
            "SELECT e.enumlabel FROM pg_type t "
            "JOIN pg_enum e ON t.oid = e.enumtypid "
            "WHERE t.typname = 'role' ORDER BY e.enumsortorder"
        ))
        current_values = [row[0] for row in result.fetchall()]

        if current_values and current_values != NEW_ROLE_VALUES:
            print(f"Migrating role enum from {current_values} to {NEW_ROLE_VALUES}")
            case_clauses = " ".join(
                f"WHEN '{old}' THEN '{LEGACY_ROLE_MAP.get(old, 'uploader')}'"
                for old in current_values
            )
            await conn.execute(text("ALTER TYPE role RENAME TO role_old"))
            await conn.execute(text(
                "CREATE TYPE role AS ENUM ('admin', 'board_secretary', 'uploader', 'board_member')"
            ))
            await conn.execute(text(
                "ALTER TABLE users ALTER COLUMN role TYPE role "
                f"USING (CASE role::text {case_clauses} ELSE 'uploader' END)::role"
            ))
            await conn.execute(text("DROP TYPE role_old"))
            print("role enum migrated.")
        else:
            print("role enum already up to date.")

    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
