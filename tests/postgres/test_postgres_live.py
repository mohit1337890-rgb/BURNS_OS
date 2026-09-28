"""
Live Postgres integration tests (marked `postgres`) - these connect to a
real running Postgres via psycopg2 and are excluded from the default
`pytest tests/unit/` run (SQLite-only, no Docker required). Run via
`make test-pg` (`docker compose exec gateway python -m pytest tests/ -v -m
postgres`), which runs INSIDE the gateway container where POSTGRES_HOST
etc. are already the real docker-compose Postgres service.

Proves things the SQLite unit tests structurally cannot:

1. KNOWN_LIMITS gap #2 - db/migrations/versions/0002_ledger_append_only.py's
   trigger actually blocks UPDATE/DELETE on `ledger` at the DB level.
   SQLite has no triggers/REVOKE in the same sense; this can only be shown
   against real Postgres with the real migration applied.
2. A genuinely concurrent race on core.approvals.mark_executing - two
   separate DB connections/sessions/threads racing on the SAME approval
   row, which SQLite's single-writer-lock model can't meaningfully
   simulate the way Postgres row-level locking under concurrent
   transactions can.
3. KNOWN_LIMITS gap #10 - the connection this test suite itself uses (via
   core.app_config.load_core_config(), same as the real Gateway) is now the
   restricted burns_app role (migration 0003), not a superuser - so
   UPDATE/DELETE being blocked above is no longer a no-op REVOKE against a
   role that bypasses ACL checks entirely, AND burns_app cannot even
   attempt the session_replication_role bypass that a superuser could
   (proved live in a disposable DB before this fix - see git history/
   docs/KNOWN_LIMITS.md for that reproduction).
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from core import app_config, approvals, ledger, policy_engine
from gateway import core_execute, plugins

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg_engine():
    core_config = app_config.load_core_config()
    if not core_config.database_url.startswith("postgresql"):
        pytest.skip("Not configured against Postgres (database_url isn't postgresql) - run via make test-pg.")
    engine = ledger.get_engine(core_config.database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session(pg_engine):
    factory = sessionmaker(bind=pg_engine, future=True)
    s = factory()
    yield s
    s.close()


@pytest.fixture
def policy():
    return policy_engine.load_policy()


@pytest.fixture(autouse=True)
def clean_registry():
    plugins._REGISTRY.clear()
    yield
    plugins._REGISTRY.clear()


class _FakePlugin:
    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def execute(self, params):
        with self._lock:
            self.calls.append(params)
        return plugins.PluginResult(ok=True, detail="fake sent", cost_usd=0.0)


def test_migrations_have_run_core_tables_exist(pg_session):
    """Confirms `alembic upgrade head` actually ran against this DB - not
    just that some create_all() happened to run (gateway/app.py deliberately
    no longer calls create_all against real Postgres - see its _lifespan).

    Deliberately does NOT check for alembic_version here - migration 0003
    never grants burns_app (this fixture's connection, since the role
    split) anything on it, so it correctly doesn't show up in this
    session's information_schema.tables view. alembic_version is a
    migration-tooling concern (burns_admin's business), not something the
    app itself needs to see.
    """
    tables = {
        row[0]
        for row in pg_session.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        )
    }
    assert {"ledger", "approvals", "missions"} <= tables


def test_ledger_insert_via_append_entry_succeeds(pg_session):
    entry = ledger.append_entry(
        pg_session,
        ledger.LedgerEntryInput(
            mission_id=None, agent_role="test", action="web_search", tier=0,
            input_summary="postgres live insert test", tool="web_search", result="EXECUTED_IN_SANDBOX",
        ),
    )
    assert entry.id is not None
    assert ledger.verify_chain(pg_session).ok


def test_direct_update_on_ledger_is_blocked_by_grant(pg_session):
    """As of migration 0003 (KNOWN_LIMITS gap #10), this connection (burns_app)
    has no UPDATE grant on ledger at all - Postgres refuses at the ACL
    check, before it would even reach the trigger. See
    test_burns_app_update_is_blocked_by_the_trigger_even_if_granted below
    for the trigger layer proved independently."""
    entry = ledger.append_entry(
        pg_session,
        ledger.LedgerEntryInput(
            mission_id=None, agent_role="test", action="web_search", tier=0,
            input_summary="row to attempt UPDATE against", tool="web_search", result="EXECUTED_IN_SANDBOX",
        ),
    )
    with pytest.raises(DBAPIError) as exc_info:
        pg_session.execute(text("UPDATE ledger SET result = 'TAMPERED' WHERE id = :id"), {"id": entry.id})
        pg_session.commit()
    pg_session.rollback()
    assert "permission denied" in str(exc_info.value).lower()


def test_direct_delete_on_ledger_is_blocked_by_grant(pg_session):
    """See test_direct_update_on_ledger_is_blocked_by_grant's docstring."""
    entry = ledger.append_entry(
        pg_session,
        ledger.LedgerEntryInput(
            mission_id=None, agent_role="test", action="web_search", tier=0,
            input_summary="row to attempt DELETE against", tool="web_search", result="EXECUTED_IN_SANDBOX",
        ),
    )
    with pytest.raises(DBAPIError) as exc_info:
        pg_session.execute(text("DELETE FROM ledger WHERE id = :id"), {"id": entry.id})
        pg_session.commit()
    pg_session.rollback()
    assert "permission denied" in str(exc_info.value).lower()


def test_burns_app_update_is_blocked_by_the_trigger_even_if_granted(pg_engine, pg_session):
    """Explicit, independent proof of Layer 2 (the trigger) - temporarily
    grants UPDATE on ledger to burns_app (as the admin role, via a separate
    connection), confirms the TRIGGER (not the ACL check) is what blocks
    the update in that state, then revokes it back. True defense-in-depth
    only matters if each layer genuinely works on its own - this is what
    the two tests above alone can't show, since burns_app never has the
    grant in real operation.

    Needs admin (burns_admin/superuser) credentials, which - correctly -
    are NOT in the gateway container's normal environment (see
    docker-compose.yml, KNOWN_LIMITS gap #10). Skips unless explicitly run
    with them injected, e.g.:
        docker compose exec -e POSTGRES_USER=<admin> -e POSTGRES_PASSWORD=<admin_pw> \\
            gateway python -m pytest tests/postgres/ -v -m postgres
    """
    if not os.environ.get("POSTGRES_USER") or not os.environ.get("POSTGRES_PASSWORD"):
        pytest.skip("Admin credentials not injected into this environment - see this test's docstring.")
    admin_url = (
        f"postgresql+psycopg2://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
    )
    admin_engine = ledger.get_engine(admin_url)
    admin_session = sessionmaker(bind=admin_engine, future=True)()
    try:
        admin_session.execute(text('GRANT UPDATE ON ledger TO "burns_app"'))
        admin_session.commit()

        entry = ledger.append_entry(
            pg_session,
            ledger.LedgerEntryInput(
                mission_id=None, agent_role="test", action="web_search", tier=0,
                input_summary="row to attempt UPDATE against, with UPDATE temporarily granted",
                tool="web_search", result="EXECUTED_IN_SANDBOX",
            ),
        )
        with pytest.raises(DBAPIError) as exc_info:
            pg_session.execute(text("UPDATE ledger SET result = 'TAMPERED' WHERE id = :id"), {"id": entry.id})
            pg_session.commit()
        pg_session.rollback()
        assert "append-only" in str(exc_info.value).lower()
    finally:
        admin_session.execute(text('REVOKE UPDATE ON ledger FROM "burns_app"'))
        admin_session.commit()
        admin_session.close()
        admin_engine.dispose()


def test_burns_app_has_no_ddl_rights_on_burns_os(pg_session):
    """Closes a gap found during the 2026-09-29 incident follow-up
    (docs/incidents/2026-09-litellm-table-drop.md): burns_app must not be
    able to CREATE/ALTER/DROP/TRUNCATE anything in burns_os's schema -
    confirmed live rather than merely relied upon from Postgres 15+'s
    default (PUBLIC no longer gets schema CREATE by default), since a
    relied-upon-but-unverified default is exactly the kind of assumption
    that contributed to this incident in the first place."""
    with pytest.raises(DBAPIError) as exc_info:
        pg_session.execute(text("CREATE TABLE evil_table (id int)"))
    pg_session.rollback()
    assert "permission denied" in str(exc_info.value).lower()


def test_litellm_app_cannot_connect_to_burns_os():
    """Closes a gap found live during the 2026-09-29 incident follow-up:
    moving litellm to its own database wasn't sufficient on its own -
    Postgres grants CONNECT on every database to PUBLIC by default, so
    litellm_app (an otherwise entirely unrelated role) could still open a
    connection to burns_os and run read queries, even with zero table
    grants there. Migration 0006 revokes PUBLIC's CONNECT. Needs
    LITELLM_DB_USER/LITELLM_DB_PASSWORD injected (not in the gateway
    container's normal environment, by design - see docker-compose.yml):
        docker compose exec -e LITELLM_DB_USER=litellm_app -e LITELLM_DB_PASSWORD=<pw> \\
            gateway python -m pytest tests/postgres/ -v -m postgres
    """
    user = os.environ.get("LITELLM_DB_USER")
    password = os.environ.get("LITELLM_DB_PASSWORD")
    if not user or not password:
        pytest.skip("LITELLM_DB_USER/LITELLM_DB_PASSWORD not injected into this environment - see this test's docstring.")
    litellm_url = f"postgresql+psycopg2://{user}:{password}@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
    engine = ledger.get_engine(litellm_url)
    with pytest.raises(DBAPIError) as exc_info:
        with engine.connect():
            pass
    engine.dispose()
    assert "permission denied" in str(exc_info.value).lower()


def test_app_connection_is_the_restricted_burns_app_role_not_a_superuser(pg_session):
    """The foundational check for KNOWN_LIMITS gap #10 - if this ever
    regresses back to a superuser role, every test below it about
    UPDATE/DELETE/session_replication_role being blocked would start
    passing for the WRONG reason (ACL/trigger bypass unavailable in
    practice) rather than the right one (genuinely not permitted)."""
    row = pg_session.execute(text("SELECT current_user, usesuper FROM pg_user WHERE usename = current_user")).first()
    current_user, is_superuser = row[0], row[1]
    assert current_user == "burns_app", f"expected to be connected as burns_app, got {current_user!r}"
    assert is_superuser is False, "burns_app must never be a Postgres superuser - see migration 0003"


def test_burns_app_cannot_bypass_the_trigger_via_session_replication_role(pg_session):
    """The exact bypass proved live against the ORIGINAL superuser-as-app-role
    setup, before this fix: a superuser can `SET session_replication_role =
    replica` to make Postgres skip normal (origin) triggers entirely,
    defeating the append-only trigger regardless of the REVOKE. Setting
    session_replication_role itself requires superuser privilege - so
    burns_app (migration 0003: NOSUPERUSER) must be refused at that SET,
    never even reaching the UPDATE/DELETE it would have enabled."""
    with pytest.raises(DBAPIError) as exc_info:
        pg_session.execute(text("SET session_replication_role = replica"))
    pg_session.rollback()
    assert "permission denied" in str(exc_info.value).lower()


def test_truncate_on_ledger_is_blocked_even_for_admin(pg_session):
    """Closes a gap found during the 2026-09-29 security-claims audit
    (migration 0005): a BEFORE ROW trigger (migration 0002) never fires
    for TRUNCATE, which is statement-level - burns_app was never granted
    TRUNCATE (so this was never exploitable by the app's own role), but
    the append-only CLAIM was incomplete without this, and it's tested
    here at the STRONGEST level (even a superuser/admin connection, not
    just burns_app) to prove the trigger itself - not a grant - is what
    stops it. This is exactly the operation used (as burns_admin) to clear
    corrupted test data during this same audit, before this migration
    closed it."""
    admin_url = (
        f"postgresql+psycopg2://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
    ) if os.environ.get("POSTGRES_USER") and os.environ.get("POSTGRES_PASSWORD") else None
    if admin_url is None:
        pytest.skip("Admin credentials not injected into this environment - see this test's docstring.")
    admin_engine = ledger.get_engine(admin_url)
    admin_session = sessionmaker(bind=admin_engine, future=True)()
    try:
        with pytest.raises(DBAPIError) as exc_info:
            admin_session.execute(text("TRUNCATE ledger"))
        admin_session.rollback()
        assert "append-only" in str(exc_info.value).lower()
    finally:
        admin_session.close()
        admin_engine.dispose()


def test_concurrent_execute_approved_action_runs_plugin_exactly_once(pg_engine, policy):
    """The genuine concurrency proof the SQLite unit test
    (test_core_execute.py::test_double_execute_approved_action_only_runs_plugin_once,
    which is sequential) can't provide - two REAL threads, each with their
    own DB session/connection, both calling execute_approved_action for the
    SAME approval_id at (as close to) the same instant as a
    threading.Barrier can arrange. core.approvals.mark_executing's single
    atomic UPDATE...WHERE is what must make this safe under real Postgres
    row-level locking (READ COMMITTED re-evaluates the WHERE clause against
    the just-committed row once the losing transaction's lock wait ends).
    """
    fake = _FakePlugin()
    plugins.register("send_message", fake)

    setup_session = sessionmaker(bind=pg_engine, future=True)()
    req = approvals.create_approval(
        setup_session, mission_id=None, agent_role="chief-of-staff", action="send_message",
        tier=2, params_summary="concurrency test", params={"text": f"concurrency-{uuid.uuid4()}"},
        expire_after_hours=24, cooling_minutes=0,
    )
    # Real configured value (core.approvals.decide_approval independently
    # verifies owner_chat_id against os.environ["TELEGRAM_OWNER_CHAT_ID"] -
    # see core/approvals.py) - not a hardcoded test double, since this runs
    # against the real container env.
    approvals.decide_approval(
        setup_session, req.id, decided_by="Mohit",
        owner_chat_id=os.environ["TELEGRAM_OWNER_CHAT_ID"], approve=True,
    )
    setup_session.close()

    barrier = threading.Barrier(2)
    results: list = [None, None]
    errors: list = []

    def worker(idx: int) -> None:
        session = sessionmaker(bind=pg_engine, future=True)()
        try:
            barrier.wait(timeout=5)
            results[idx] = core_execute.execute_approved_action(session, req.id)
        except Exception as exc:  # noqa: BLE001 - surfaced via `errors` for a clear assertion message
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"worker thread(s) raised: {errors}"
    statuses = sorted(r.status for r in results if r is not None)
    assert statuses == ["executed", "not_executed"], f"got statuses={statuses}, results={results}"
    assert len(fake.calls) == 1, f"plugin.execute() ran {len(fake.calls)} times, expected exactly 1 - {fake.calls}"

    # Both threads' ledger writes (the EXECUTING marker, the final OK/FAILED
    # result, and the loser's NOT_EXECUTED entry) land through
    # core.ledger.append_entry() concurrently - this is the exact scenario
    # that found the prev_hash race live (2026-09-29, core/ledger.py's
    # uq_ledger_prev_hash). A broken chain here would mean that fix regressed.
    verify_session = sessionmaker(bind=pg_engine, future=True)()
    assert ledger.verify_chain(verify_session).ok
    verify_session.close()
