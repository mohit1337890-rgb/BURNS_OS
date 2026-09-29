from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import pytest

from core import approvals, ledger, reconciliation
from core.approval_channels import TelegramChannel
from core.approvals import ApprovalStatus

OWNER_CHAT_ID = "recon-owner"
TELEGRAM_CHANNEL = TelegramChannel()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", OWNER_CHAT_ID)
    yield


# `session` (parametrized sqlite/postgres) comes from tests/unit/conftest.py.


def _approved_request(session, *, tier=2, cooling_minutes=0):
    req = approvals.create_approval(
        session, mission_id=None, agent_role="chief-of-staff", action="send_message", tier=tier,
        params_summary="reconciliation test", params={"text": "hi"},
        expire_after_hours=24, cooling_minutes=cooling_minutes,
    )
    approvals.decide_approval(session, req.id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=True, chat_id=OWNER_CHAT_ID)
    return req


# --- reconcile_stuck_approval (item 3 - manual, audited, admin-only) -------

def test_reconcile_stuck_approval_moves_status_and_appends_ledger_entry(session):
    req = _approved_request(session)
    ok = approvals.mark_executing(session, req.id)  # simulates the pre-fix bug: claimed, then nothing else ever ran
    assert ok is True

    entry = reconciliation.reconcile_stuck_approval(
        session, req.id, reason="simulated pre-EXECUTING-marker crash", reconciled_by="test-admin",
    )

    assert "RECONCILED_FAILED" in entry.result
    assert "test-admin" in entry.result
    updated = approvals.get_approval(session, req.id)
    assert updated.status == ApprovalStatus.FAILED_RECONCILED.value


def test_reconcile_stuck_approval_ignores_the_normal_pending_approval_row(session):
    """Regression test for a bug caught live (2026-09-29) reconciling the
    real d0521604 approval: every Tier-2/3 approval gets a normal
    PENDING_APPROVAL ledger row at request time (gateway/core_execute.py::
    request_action) - reconcile_stuck_approval() must not mistake that for
    an existing execution record and refuse."""
    req = _approved_request(session)
    ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=None, agent_role="x", action="send_message", tier=2,
            input_summary="normal request-time row", tool="send_message", result="PENDING_APPROVAL", approval_id=req.id,
        ),
    )
    approvals.mark_executing(session, req.id)

    entry = reconciliation.reconcile_stuck_approval(session, req.id, reason="x", reconciled_by="test-admin")
    assert "RECONCILED_FAILED" in entry.result


def test_reconcile_stuck_approval_refuses_when_not_executed(session):
    req = _approved_request(session)  # APPROVED, never claimed
    with pytest.raises(ValueError, match="not EXECUTED"):
        reconciliation.reconcile_stuck_approval(session, req.id, reason="x", reconciled_by="test-admin")


def test_reconcile_stuck_approval_refuses_when_ledger_rows_already_exist(session):
    req = _approved_request(session)
    approvals.mark_executing(session, req.id)
    ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=None, agent_role="x", action="send_message", tier=2,
            input_summary="already has a row", tool="send_message", result="OK: sent", approval_id=req.id,
        ),
    )
    with pytest.raises(ValueError, match="already has"):
        reconciliation.reconcile_stuck_approval(session, req.id, reason="x", reconciled_by="test-admin")


# --- find_stale_executing_markers / mark_unknown_outcome / run_reconciliation_pass (item 4) --

def test_find_stale_executing_markers_finds_old_unresolved_marker(session):
    req = _approved_request(session)
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=30)
    ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=None, agent_role="x", action="send_message", tier=2,
            input_summary="stale", tool="send_message", result="EXECUTING", approval_id=req.id,
        ),
        ts=old_ts,
    )
    stale = reconciliation.find_stale_executing_markers(session, older_than_minutes=15)
    assert len(stale) == 1
    assert stale[0].approval_id == req.id


def test_find_stale_executing_markers_skips_recent_marker(session):
    req = _approved_request(session)
    ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=None, agent_role="x", action="send_message", tier=2,
            input_summary="fresh", tool="send_message", result="EXECUTING", approval_id=req.id,
        ),
    )
    stale = reconciliation.find_stale_executing_markers(session, older_than_minutes=15)
    assert stale == []


def test_find_stale_executing_markers_skips_resolved_marker(session):
    req = _approved_request(session)
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=30)
    ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=None, agent_role="x", action="send_message", tier=2,
            input_summary="resolved", tool="send_message", result="EXECUTING", approval_id=req.id,
        ),
        ts=old_ts,
    )
    ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=None, agent_role="x", action="send_message", tier=2,
            input_summary="resolved", tool="send_message", result="OK: sent", approval_id=req.id,
        ),
    )
    stale = reconciliation.find_stale_executing_markers(session, older_than_minutes=15)
    assert stale == []


def test_run_reconciliation_pass_marks_unknown_outcome_and_alerts(session, capsys):
    req = _approved_request(session)
    approvals.mark_executing(session, req.id)
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=30)
    ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=None, agent_role="x", action="send_message", tier=2,
            input_summary="stuck mid-execution", tool="send_message", result="EXECUTING", approval_id=req.id,
        ),
        ts=old_ts,
    )

    entries = reconciliation.run_reconciliation_pass(session, older_than_minutes=15)

    assert len(entries) == 1
    assert "UNKNOWN_OUTCOME" in entries[0].result
    updated = approvals.get_approval(session, req.id)
    assert updated.status == ApprovalStatus.UNKNOWN_OUTCOME.value
    assert "RECONCILIATION ALERT" in capsys.readouterr().out


# --- genuine OS-level process kill (item 4's explicit ask: "a real kill, e.g. os._exit, in a subprocess") --

def test_real_process_kill_mid_plugin_call_leaves_a_recoverable_executing_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", OWNER_CHAT_ID)
    """The try/except in execute_approved_action cannot catch this class of
    failure at all - os._exit() terminates the interpreter immediately,
    skipping exception handling, finally blocks, and atexit hooks entirely.
    Only the EXECUTING marker (committed to the DB BEFORE the plugin is
    even called) can make this recoverable. Uses a real file-backed SQLite
    DB (not :memory:) so a genuinely separate OS process can share it.
    """
    db_path = tmp_path / "kill_test.db"
    db_url = f"sqlite:///{db_path}"

    engine = ledger.get_engine(db_url)
    ledger.init_db(engine)
    session = ledger.get_session_factory(engine)()
    req = approvals.create_approval(
        session, mission_id=None, agent_role="chief-of-staff", action="send_message", tier=2,
        params_summary="kill test", params={"text": "hi"}, expire_after_hours=24, cooling_minutes=0,
    )
    approvals.decide_approval(session, req.id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=True, chat_id=OWNER_CHAT_ID)
    session.close()

    worker_script = textwrap.dedent(f"""
        import os
        from core import ledger
        from gateway import core_execute, plugins

        class _KillPlugin:
            def execute(self, params):
                os._exit(17)  # real, immediate OS-level termination - no Python cleanup at all

        plugins.register("send_message", _KillPlugin())
        engine = ledger.get_engine({db_url!r})
        session = ledger.get_session_factory(engine)()
        core_execute.execute_approved_action(session, {req.id!r})
        print("UNREACHABLE - os._exit should have terminated the process already")
    """)

    result = subprocess.run([sys.executable, "-c", worker_script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 17, f"expected os._exit(17), got returncode={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    assert "UNREACHABLE" not in result.stdout

    session = ledger.get_session_factory(engine)()
    updated = approvals.get_approval(session, req.id)
    assert updated.status == ApprovalStatus.EXECUTED.value  # mark_executing() committed before the kill

    rows = session.query(ledger.LedgerEntry).filter_by(approval_id=req.id).order_by(ledger.LedgerEntry.id.asc()).all()
    assert len(rows) == 1
    assert rows[0].result == "EXECUTING"  # the marker survived; no final result ever got written

    reconciled = reconciliation.run_reconciliation_pass(session, older_than_minutes=0)
    assert len(reconciled) == 1
    assert "UNKNOWN_OUTCOME" in reconciled[0].result
    final = approvals.get_approval(session, req.id)
    assert final.status == ApprovalStatus.UNKNOWN_OUTCOME.value
    session.close()
