"""Alembic environment. Builds its OWN admin database URL directly from
POSTGRES_HOST/PORT/DB/USER/PASSWORD (the Postgres superuser - see
.env/.env.example) rather than going through core.app_config.load_core_config(),
whose database_url is deliberately the restricted burns_app role instead
(core/app_config.py - closes KNOWN_LIMITS gap #10). Migrations need real
DDL rights (CREATE TABLE/ROLE/TRIGGER, REVOKE/GRANT) that burns_app must
never have, so "the same URL the Gateway connects with" is exactly what
this must NOT use, unlike before the role split.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.approvals import ApprovalRequest  # noqa: E402,F401 - imported for Base.metadata registration
from core.ledger import Base  # noqa: E402
from core.missions import Mission  # noqa: E402,F401 - imported for Base.metadata registration

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_database_url() -> str:
    missing = [v for v in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER") if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            "Missing required admin-DB environment variable(s) for running migrations: " + ", ".join(missing) +
            " - these are the burns_admin/superuser credentials (see .env.example), separate from "
            "BURNS_APP_DB_USER/PASSWORD which core/app_config.py uses for the app's own restricted connection."
        )
    user = os.environ["POSTGRES_USER"]
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ["POSTGRES_HOST"]
    port = os.environ["POSTGRES_PORT"]
    db = os.environ["POSTGRES_DB"]
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"


def run_migrations_offline() -> None:
    url = _get_database_url()
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _get_database_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
