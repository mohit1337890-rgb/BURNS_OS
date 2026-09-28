"""Ledger append-only: also block TRUNCATE, not just UPDATE/DELETE.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-29

Found during the 2026-09-29 security-claims audit (triggered by needing to
clear corrupted synthetic test data live via TRUNCATE, as burns_admin,
while investigating the litellm shared-database incident): migration
0002's trigger only covers row-level UPDATE/DELETE - a BEFORE ROW trigger
never fires for TRUNCATE, which operates at the statement level. Since
burns_app has no TRUNCATE grant (migration 0003 never granted it), this
was never exploitable by the app's own restricted role - but the
append-only CLAIM itself ("every action is logged, permanently") was
incomplete without this, and any future role/grant change could otherwise
silently reopen it. TRUNCATE triggers can't reference OLD/NEW (there's no
per-row data), so this uses its own function and a FOR EACH STATEMENT
trigger rather than reusing ledger_block_mutation().
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION ledger_block_truncate() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'ledger is append-only: TRUNCATE is not permitted (Burns OS design rule 6: every action is logged, permanently)';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER ledger_no_truncate
        BEFORE TRUNCATE ON ledger
        FOR EACH STATEMENT EXECUTE FUNCTION ledger_block_truncate();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ledger_no_truncate ON ledger")
    op.execute("DROP FUNCTION IF EXISTS ledger_block_truncate()")
