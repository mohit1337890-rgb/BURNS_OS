"""Ledger append-only enforcement (closes KNOWN_LIMITS gap #2)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28

WRITTEN, NOT YET RUN - no Postgres instance available on this dev machine
to verify against (see docs/KNOWN_LIMITS.md). Two independent layers, on
the theory that a single mechanism is a single point of failure:

1. REVOKE UPDATE/DELETE on the `ledger` table from the app's own role - so
   even a full SQL-injection-level compromise of the Gateway's own DB
   credentials can't rewrite history through the connection it actually
   has.
2. A trigger that RAISEs on any UPDATE/DELETE attempt against `ledger`,
   regardless of role - so even a superuser/migration-script mistake
   (someone with more privilege than the app's own role) gets a loud,
   named error instead of silently succeeding.

The app's Postgres role name is read from POSTGRES_USER (matches
core/app_config.py's own required env var) rather than hardcoded, so this
migration works whatever the real deployment names that role.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    app_role = bind.engine.url.username

    # Layer 1: role-level privilege revocation.
    op.execute(f'REVOKE UPDATE, DELETE ON TABLE ledger FROM "{app_role}"')

    # Layer 2: a trigger that blocks UPDATE/DELETE outright, independent of
    # who's connected - INSERT remains the only permitted write.
    op.execute("""
        CREATE OR REPLACE FUNCTION ledger_block_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'ledger is append-only: % on ledger.id=% is not permitted (Burns OS design rule 6: every action is logged, permanently)',
                TG_OP, COALESCE(OLD.id, NEW.id);
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER ledger_no_update
        BEFORE UPDATE ON ledger
        FOR EACH ROW EXECUTE FUNCTION ledger_block_mutation();
    """)
    op.execute("""
        CREATE TRIGGER ledger_no_delete
        BEFORE DELETE ON ledger
        FOR EACH ROW EXECUTE FUNCTION ledger_block_mutation();
    """)


def downgrade() -> None:
    bind = op.get_bind()
    app_role = bind.engine.url.username

    op.execute("DROP TRIGGER IF EXISTS ledger_no_delete ON ledger")
    op.execute("DROP TRIGGER IF EXISTS ledger_no_update ON ledger")
    op.execute("DROP FUNCTION IF EXISTS ledger_block_mutation()")
    op.execute(f'GRANT UPDATE, DELETE ON TABLE ledger TO "{app_role}"')
