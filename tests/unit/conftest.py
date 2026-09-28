"""
Shared `session` fixture, parametrized over ["sqlite", "postgres"] - item 6
of the 2026-09-29 live-testing follow-up ("run the FULL test suite against
Postgres too, via a DB fixture parametrized sqlite/postgres"). Any test
file in this directory that accepts a `session` argument now runs TWICE:
once against a fresh in-memory SQLite DB (as before), and once against the
real Postgres instance (docker-compose's `postgres` service, reachable at
POSTGRES_HOST from this machine since docker-compose publishes it to
127.0.0.1:5432) - connecting as burns_app, same role the real Gateway
uses (core/app_config.py, KNOWN_LIMITS gap #10).

The Postgres half is skipped automatically (not failed) when unreachable -
CI or a dev machine without `make up` running still gets the SQLite half.

Per-test isolation on the Postgres side uses the standard SQLAlchemy
"join a Session into an external transaction" recipe: everything the test
(and any core.ledger.append_entry() retry-on-collision rollback inside it)
does happens inside a SAVEPOINT that's discarded at teardown via rolling
back the OUTER transaction - the test never actually commits anything
Postgres-visible to other connections, so tests that assert on total row
counts (e.g. tests/unit/test_ledger.py) see a pristine table every time,
identical to the SQLite half's fresh in-memory DB.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core import approvals, ledger, missions  # noqa: F401 - approvals/missions must be imported so their
# tables register on the SHARED Base.metadata before ledger.init_db()'s
# create_all() runs below; conftest.py is imported very early in pytest's
# collection (before most test files' own `from core import approvals`
# lines run), so relying on import order elsewhere isn't enough here -
# found live (2026-09-29): the Postgres half failed every approvals/
# missions-touching test with "relation approvals does not exist" because
# only the ledger table existed in burns_os_test.

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_dotenv_once() -> None:
    """Tests shouldn't require the developer to have manually exported
    .env - only fills in vars not already set (a real CI environment's own
    exported vars always win)."""
    env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    for k, v in dotenv_values(env_path).items():
        if v is not None and k not in os.environ:
            os.environ[k] = v


_load_dotenv_once()


def _postgres_url() -> str | None:
    host = os.environ.get("POSTGRES_HOST")
    user = os.environ.get("BURNS_APP_DB_USER")
    if not host or not user:
        return None
    password = os.environ.get("BURNS_APP_DB_PASSWORD", "")
    port = os.environ.get("POSTGRES_PORT", "5432")
    # A SEPARATE database from POSTGRES_DB (burns_os, the "live" one this
    # session's manual bring-up testing has been writing real demo/evidence
    # rows into) - found live (2026-09-29): SAVEPOINT-based rollback only
    # isolates a test's OWN new writes, it does NOT hide rows some earlier,
    # already-committed transaction left behind, so genesis/empty-ledger
    # assumptions (test_ledger.py::test_first_entry_chains_from_genesis
    # etc.) failed the first time this pointed at burns_os. Created once,
    # manually, by an admin (`CREATE DATABASE burns_os_test`) - same
    # reasoning as burns_app/litellm_db not being auto-created here.
    db = os.environ.get("POSTGRES_TEST_DB", "burns_os_test")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db}"


def _postgres_reachable_and_ready(url: str) -> bool:
    try:
        engine = create_engine(url, future=True)
        with engine.connect():
            pass
        ledger.init_db(engine)  # idempotent - create_all() no-ops on tables that already exist
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001 - any connection failure just means "skip this backend"
        return False


_PG_URL = _postgres_url()
_PG_AVAILABLE = bool(_PG_URL) and _postgres_reachable_and_ready(_PG_URL)


@pytest.fixture(params=["sqlite", "postgres"])
def db_backend(request):
    if request.param == "postgres" and not _PG_AVAILABLE:
        pytest.skip(
            "Postgres not reachable via BURNS_APP_DB_USER@POSTGRES_HOST - "
            "run `make up` (or `docker compose up -d postgres`) for the Postgres half. See tests/unit/conftest.py."
        )
    return request.param


@pytest.fixture
def session(db_backend):
    if db_backend == "sqlite":
        engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
        ledger.init_db(engine)
        factory = sessionmaker(bind=engine, future=True)
        s = factory()
        yield s
        s.close()
        engine.dispose()
        return

    engine = create_engine(_PG_URL, future=True)
    connection = engine.connect()
    outer_transaction = connection.begin()
    factory = sessionmaker(bind=connection, future=True)
    s = factory()
    s.begin_nested()  # SAVEPOINT

    @event.listens_for(s, "after_transaction_end")
    def _restart_savepoint(sess, transaction):
        # Every session.commit() the app code does under test only ends
        # the current SAVEPOINT - restart one immediately so the NEXT
        # commit/rollback has something to act on, without ever touching
        # outer_transaction (which is what actually gets discarded below).
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    yield s

    s.close()
    outer_transaction.rollback()
    connection.close()
    engine.dispose()
