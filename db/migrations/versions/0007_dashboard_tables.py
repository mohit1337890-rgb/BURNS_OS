"""Dashboard tables: owner_account, dashboard_session, command_request.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-29

The Web Dashboard becomes the primary approval channel (Telegram
postponed - see core/approval_channels.py, docs/KNOWN_LIMITS.md). These
are mutable application state (current password/TOTP secret, active
sessions), NOT audit history - unlike `ledger`, burns_app gets full CRUD
here, same as `approvals`/`missions`.

owner_account is deliberately a single-row table (one owner, checked in
application code, not a DB constraint - a UNIQUE index on username
already prevents a second row with the same username, which is the
practical concern; a hard "exactly one row ever" constraint isn't worth
the complexity for a system that only ever runs one owner).

dashboard_session.id stores a SHA-256 hash of the raw session token, not
the token itself - a database dump/leak alone must not be enough to
hijack a session, the same reasoning as never storing a plaintext
password.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "owner_account",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("username", sa.String, nullable=False),
        sa.Column("password_hash", sa.String, nullable=False),
        sa.Column("totp_secret", sa.String, nullable=True),
        sa.Column("totp_enrolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failed_login_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_owner_account_username", "owner_account", ["username"], unique=True)

    op.create_table(
        "dashboard_session",
        sa.Column("id", sa.String, primary_key=True),  # SHA-256 hex of the raw token - see module docstring
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("csrf_secret", sa.String, nullable=False),
        sa.Column("ip_address", sa.String, nullable=True),
        sa.Column("user_agent", sa.String, nullable=True),
    )

    op.create_table(
        "command_request",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("text", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ledger_entry_id", sa.Integer, nullable=True),
    )

    bind = op.get_bind()
    app_role = "burns_app"
    for table in ("owner_account", "dashboard_session", "command_request"):
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{app_role}"')
    op.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{app_role}"')


def downgrade() -> None:
    op.drop_table("command_request")
    op.drop_table("dashboard_session")
    op.drop_index("ix_owner_account_username", table_name="owner_account")
    op.drop_table("owner_account")
