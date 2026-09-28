"""Revoke PUBLIC's default CONNECT privilege on burns_os.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29

Found live during the 2026-09-29 incident follow-up
(docs/incidents/2026-09-litellm-table-drop.md): Postgres grants CONNECT on
every database to the special PUBLIC pseudo-role by default, unless
explicitly revoked - migration 0003 granted burns_app CONNECT but never
revoked the PUBLIC default, so litellm_app (created separately, with no
intentional relationship to burns_os at all) could still open a
connection and run read queries against burns_os, even though it holds no
table-level grants there. `SELECT 1` succeeding was the proof; a real
table read would have needed a table grant too (none exist), but CONNECT
alone is more access than any role outside burns_os's own app/admin roles
should have. This revokes PUBLIC's CONNECT, then re-grants it explicitly
to the two roles that need it (burns_app, burns_admin already has it as
superuser).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    db_name = bind.engine.url.database
    op.execute(f'REVOKE CONNECT ON DATABASE "{db_name}" FROM PUBLIC')
    op.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO "burns_app"')


def downgrade() -> None:
    bind = op.get_bind()
    db_name = bind.engine.url.database
    op.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO PUBLIC')
