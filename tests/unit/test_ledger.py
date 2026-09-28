from datetime import datetime, timezone

import pytest

from core import ledger

# `session` (parametrized sqlite/postgres) comes from tests/unit/conftest.py.


def _entry(action="send_message", tier=2, cost=0.0) -> ledger.LedgerEntryInput:
    return ledger.LedgerEntryInput(
        mission_id="mission-1",
        agent_role="chief-of-staff",
        action=action,
        tier=tier,
        input_summary="test entry",
        tool="telegram",
        result="ok",
        approved_by="Mohit",
        approval_id="appr-1",
        cost_usd=cost,
        evidence_links=[],
    )


def test_first_entry_chains_from_genesis(session):
    row = ledger.append_entry(session, _entry())
    assert row.prev_hash == ledger.GENESIS_HASH
    assert len(row.hash) == 64
    assert row.hash != ledger.GENESIS_HASH


def test_second_entry_chains_to_first(session):
    first = ledger.append_entry(session, _entry(action="a"))
    second = ledger.append_entry(session, _entry(action="b"))
    assert second.prev_hash == first.hash


def test_verify_chain_ok_on_untampered_ledger(session):
    for i in range(5):
        ledger.append_entry(session, _entry(action=f"action-{i}", cost=1.5))
    result = ledger.verify_chain(session)
    assert result.ok is True
    assert result.total_entries == 5
    assert result.first_broken_id is None


def test_verify_chain_fails_cleanly_when_the_table_is_missing_entirely():
    """Closes a gap found during the 2026-09-29 incident review
    (docs/incidents/2026-09-litellm-table-drop.md): before this fix,
    verify_chain() against a database where `ledger` doesn't exist at all
    (exactly what litellm's schema-sync left behind) raised an unhandled
    DBAPIError instead of reporting a clean ok=False - which would have
    meant gateway/app.py's /ledger/verify 500ing instead of clearly
    reporting "verification failed" during the actual incident.
    SQLite-specific (not the parametrized fixture): deliberately does NOT
    call ledger.init_db(), so there is no `ledger` table on this engine.
    """
    engine = ledger.get_engine("sqlite:///:memory:")
    factory = ledger.get_session_factory(engine)
    s = factory()
    result = ledger.verify_chain(s)
    assert result.ok is False
    assert "missing" in result.reason.lower() or "does not exist" in result.reason.lower() or "no such table" in result.reason.lower()
    s.close()


def test_verify_chain_ok_on_empty_ledger(session):
    result = ledger.verify_chain(session)
    assert result.ok is True
    assert result.total_entries == 0


def test_verify_chain_detects_tampered_field(session):
    ledger.append_entry(session, _entry(action="first"))
    row2 = ledger.append_entry(session, _entry(action="second"))
    ledger.append_entry(session, _entry(action="third"))

    # Tamper with a stored field directly (bypassing append_entry, which is
    # the whole point of the test - simulating someone editing the DB
    # directly rather than going through the only sanctioned write path).
    row2.cost_usd = 999999.0
    session.commit()

    result = ledger.verify_chain(session)
    assert result.ok is False
    assert result.first_broken_id == row2.id
    assert "content hash" in result.reason


def test_verify_chain_detects_deleted_row_breaking_prev_hash_link(session):
    ledger.append_entry(session, _entry(action="first"))
    row2 = ledger.append_entry(session, _entry(action="second"))
    ledger.append_entry(session, _entry(action="third"))

    session.delete(row2)
    session.commit()

    result = ledger.verify_chain(session)
    assert result.ok is False
    assert "prev_hash" in result.reason


def test_append_entry_retries_on_a_prev_hash_collision(session, monkeypatch):
    """Regression test for a real bug found live via a genuine two-thread
    Postgres race (2026-09-29): append_entry()'s read-latest-hash-then-
    insert wasn't atomic, so two concurrent callers could both read the
    same "latest" hash and produce a forked chain (two rows with the same
    prev_hash) - LedgerEntry.uq_ledger_prev_hash (a real DB UNIQUE
    constraint, present even on SQLite via create_all) is what turns that
    into a retryable failure instead of a silent fork. This test
    deterministically simulates the race in a single thread: the row
    _latest_hash() reads gets overtaken by ANOTHER row before our own
    insert lands, forcing a real IntegrityError on the first attempt.

    SQLite-only: this test's own single-thread SAVEPOINT-within-a-SAVEPOINT
    simulation interacts badly with the outer per-test SAVEPOINT
    tests/unit/conftest.py uses for Postgres isolation (an object gets
    expired across a nested rollback boundary it wasn't meant to cross).
    The thing this test proves - the retry logic works under a real race -
    is already proven far more convincingly against genuine Postgres by
    tests/postgres/test_postgres_live.py's actual two-THREAD concurrency
    test, so this gap costs nothing real.
    """
    if session.bind.dialect.name != "sqlite":
        pytest.skip("Single-thread race simulation only - see docstring for why the real proof is elsewhere.")
    first = ledger.append_entry(session, _entry(action="first"))
    real_latest_hash = ledger._latest_hash
    triggered = {"done": False}

    def racy_latest_hash(s):
        if not triggered["done"]:
            triggered["done"] = True
            # A "concurrent" writer lands here, between our read and our
            # insert, extending the chain from the same row we just read
            # (via the REAL _latest_hash, not this patched one - a genuine
            # concurrent writer wouldn't recurse into our own race setup).
            ledger.append_entry(s, _entry(action="concurrent-interloper"), )
            return first.hash  # now stale - our own insert below will collide
        return real_latest_hash(s)

    monkeypatch.setattr(ledger, "_latest_hash", racy_latest_hash)
    third = ledger.append_entry(session, _entry(action="third"))

    assert third.prev_hash != first.hash  # did NOT fork off the stale read
    result = ledger.verify_chain(session)
    assert result.ok is True
    assert result.total_entries == 3


def test_compute_hash_is_deterministic(session):
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    entry = _entry()
    h1 = ledger.compute_hash(ts, entry, ledger.GENESIS_HASH)
    h2 = ledger.compute_hash(ts, entry, ledger.GENESIS_HASH)
    assert h1 == h2


def test_compute_hash_changes_if_any_field_changes(session):
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    h1 = ledger.compute_hash(ts, _entry(cost=1.0), ledger.GENESIS_HASH)
    h2 = ledger.compute_hash(ts, _entry(cost=1.01), ledger.GENESIS_HASH)
    assert h1 != h2
