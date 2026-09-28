"""Separate burns_app role: closes KNOWN_LIMITS gap #10.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29

Found live during STEP 1 Postgres testing (2026-09-28): migration 0002's
REVOKE UPDATE/DELETE and its append-only trigger both assume the app's own
Postgres role is NOT a superuser - but POSTGRES_USER (the role the app
actually connected as) is also the Postgres superuser created by the
official image's own bootstrap, since it's the same variable. A superuser
bypasses ALL ACL checks (REVOKE is a no-op against it) and can bypass even
the trigger via `SET session_replication_role = replica` (superuser-only,
but that's exactly the point - if the role that IS the app's own runtime
credential also happens to be a superuser, that "superuser-only" guard
protects nothing). Proved live in a disposable test DB before this fix.

This migration creates burns_app: NOSUPERUSER, NOCREATEDB, NOCREATEROLE,
NOREPLICATION, only SELECT+INSERT on ledger (no UPDATE/DELETE - explicit
REVOKE below is belt-and-braces, since the default is no grant at all),
full CRUD on approvals/missions (both are legitimately mutable - only the
ledger is append-only). From here on, migrations run as the ORIGINAL
POSTGRES_USER (now conceptually "burns_admin" - see .env/.env.example),
while gateway/approvals_bot/scheduler connect as burns_app
(BURNS_APP_DB_USER/BURNS_APP_DB_PASSWORD - core/app_config.py).

The role is created/updated idempotently (CREATE ... IF NOT EXISTS has no
direct equivalent for ROLE, hence the DO block) so re-running this
migration after rotating BURNS_APP_DB_PASSWORD just updates the password
rather than failing.
"""
from __future__ import annotations

import os
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _app_role() -> tuple[str, str]:
    user = os.environ["BURNS_APP_DB_USER"]
    password = os.environ["BURNS_APP_DB_PASSWORD"]
    # Defensive escaping for the SQL string literal - both values come from
    # our own .env, not external input, but this costs nothing.
    return user.replace('"', ""), password.replace("'", "''")


def upgrade() -> None:
    app_user, app_password = _app_role()
    bind = op.get_bind()
    db_name = bind.engine.url.database

    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{app_user}') THEN
                CREATE ROLE "{app_user}" WITH LOGIN PASSWORD '{app_password}'
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
            ELSE
                ALTER ROLE "{app_user}" WITH LOGIN PASSWORD '{app_password}'
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
            END IF;
        END
        $$;
    """)

    op.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO "{app_user}"')
    op.execute(f'GRANT USAGE ON SCHEMA public TO "{app_user}"')

    op.execute(f'GRANT SELECT, INSERT ON ledger TO "{app_user}"')
    op.execute(f'REVOKE UPDATE, DELETE ON ledger FROM "{app_user}"')  # explicit, even though the default is no grant

    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON approvals TO "{app_user}"')
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON missions TO "{app_user}"')

    op.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{app_user}"')


def downgrade() -> None:
    app_user, _ = _app_role()
    op.execute(f'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{app_user}"')
    op.execute(f'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{app_user}"')
    op.execute(f'REVOKE USAGE ON SCHEMA public FROM "{app_user}"')
    op.execute(f'REVOKE CONNECT ON DATABASE "{op.get_bind().engine.url.database}" FROM "{app_user}"')
    op.execute(f'DROP ROLE IF EXISTS "{app_user}"')
