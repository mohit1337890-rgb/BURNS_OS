"""command_request: status + result_text (Milestone 2 dispatch tracking).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-29

Milestone 2 (docs/MILESTONE_2_HERMES_DESIGN.md): the Command box now
actually dispatches to hermes-chief (fire-and-forget), so command_request
needs somewhere to track that. status defaults to 'logged_only' for every
existing row - before this column existed, nothing was ever dispatched to
anything, so backfilling anything else would be inaccurate."""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("command_request", sa.Column("status", sa.String, nullable=False, server_default="logged_only"))
    op.add_column("command_request", sa.Column("result_text", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("command_request", "result_text")
    op.drop_column("command_request", "status")
