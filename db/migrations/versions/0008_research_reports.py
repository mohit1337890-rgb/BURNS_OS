"""research_report: the researcher -> chief-of-staff handoff store.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-29

Milestone 2 (docs/MILESTONE_2_HERMES_DESIGN.md, Mohit's approval
condition 4): submit_research_report/get_research_report are ordinary
Tier-0 Gateway actions - this table is their persistence, not a new
channel between the two Hermes containers. report_text_untrusted is
ALREADY wrapped in the UNTRUSTED marker by the time it's written here
(gateway/plugins/web_research.py) - stored as Text since a research
report can be long. burns_app gets full CRUD, same as command_request in
migration 0007 - mutable application state, not append-only audit
history (that's still core.ledger's job)."""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_report",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("command_request_id", sa.String, nullable=True),
        sa.Column("query", sa.String, nullable=False),
        sa.Column("report_text_untrusted", sa.Text, nullable=False),
        sa.Column("source_urls", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_research_report_command_request_id", "research_report", ["command_request_id"])

    bind = op.get_bind()
    app_role = "burns_app"
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON research_report TO "{app_role}"')
    op.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{app_role}"')


def downgrade() -> None:
    op.drop_index("ix_research_report_command_request_id", table_name="research_report")
    op.drop_table("research_report")
