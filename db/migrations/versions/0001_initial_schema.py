"""Initial schema: ledger, approvals, missions

Revision ID: 0001
Revises:
Create Date: 2026-09-28

WRITTEN, NOT YET RUN against a real Postgres instance - see
docs/KNOWN_LIMITS.md. Columns mirror core/ledger.py::LedgerEntry,
core/approvals.py::ApprovalRequest, core/missions.py::Mission exactly -
keep these in sync if the SQLAlchemy models change (Alembic autogenerate
would normally do this once a real DB exists to diff against).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ledger",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mission_id", sa.String, nullable=True),
        sa.Column("agent_role", sa.String, nullable=False),
        sa.Column("action", sa.String, nullable=False),
        sa.Column("tier", sa.Integer, nullable=False),
        sa.Column("input_summary", sa.String, nullable=False),
        sa.Column("tool", sa.String, nullable=False),
        sa.Column("approved_by", sa.String, nullable=True),
        sa.Column("approval_id", sa.String, nullable=True),
        sa.Column("result", sa.String, nullable=False),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("evidence_links", sa.JSON, nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
    )
    op.create_index("ix_ledger_mission_id", "ledger", ["mission_id"])
    op.create_index("ix_ledger_approval_id", "ledger", ["approval_id"])
    op.create_index("ix_ledger_ts", "ledger", ["ts"])

    op.create_table(
        "approvals",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("mission_id", sa.String, nullable=True),
        sa.Column("agent_role", sa.String, nullable=False),
        sa.Column("action", sa.String, nullable=False),
        sa.Column("tier", sa.Integer, nullable=False),
        sa.Column("params_summary", sa.String, nullable=False),
        sa.Column("params", sa.JSON, nullable=False),
        sa.Column("params_hash", sa.String(64), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String, nullable=False, server_default="PENDING"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String, nullable=True),
        sa.Column("cooling_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_approvals_status", "approvals", ["status"])
    op.create_index("ix_approvals_mission_id", "approvals", ["mission_id"])

    op.create_table(
        "missions",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False, server_default="INTAKE"),
        sa.Column("spec", sa.JSON, nullable=True),
        sa.Column("budget_usd", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_reason", sa.String, nullable=True),
        sa.Column("spec_approval_id", sa.String, nullable=True),
    )
    op.create_index("ix_missions_status", "missions", ["status"])


def downgrade() -> None:
    op.drop_table("missions")
    op.drop_table("approvals")
    op.drop_table("ledger")
