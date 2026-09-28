"""Ledger prev_hash UNIQUE constraint - DB-enforced hash-chain safety under
genuinely concurrent writers.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-29

Found live (2026-09-29): core.ledger.append_entry()'s read-latest-hash-then-
insert was not atomic - two genuinely concurrent callers (the same
execute_approved_action race the Postgres concurrency test deliberately
constructs) could both read the same "latest" row before either committed,
producing two ledger rows with the SAME prev_hash - a forked chain that
verify_chain() correctly detects, but only after the fact. A UNIQUE
constraint on prev_hash makes the second writer's INSERT fail at the DB
level instead of silently succeeding into a fork; core.ledger.append_entry()
now retries on that specific failure.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint("uq_ledger_prev_hash", "ledger", ["prev_hash"])


def downgrade() -> None:
    op.drop_constraint("uq_ledger_prev_hash", "ledger", type_="unique")
