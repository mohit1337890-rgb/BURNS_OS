"""
Tests for dashboard/reconciliation.py - the CommandRequest analogue of
core/reconciliation.py's EXECUTING-marker pattern, for a hermes-chief (or
the dispatching process) dying mid-task (docs/evidence/
milestone2_step5_command_box_dispatch.md's flagged gap, closed here).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from dashboard import reconciliation as command_reconciliation
from dashboard.models import CommandRequest


def _make_command(session, *, status: str, created_at: datetime, text: str = "test command") -> CommandRequest:
    cmd = CommandRequest(id=str(uuid.uuid4()), text=text, created_at=created_at, status=status)
    session.add(cmd)
    session.commit()
    return cmd


def test_find_stale_dispatched_commands_finds_old_unresolved(session):
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=30)
    cmd = _make_command(session, status="dispatched", created_at=old_ts)

    stale = command_reconciliation.find_stale_dispatched_commands(session, older_than_minutes=15)

    assert len(stale) == 1
    assert stale[0].command_request_id == cmd.id


def test_find_stale_dispatched_commands_skips_recent(session):
    _make_command(session, status="dispatched", created_at=datetime.now(timezone.utc))
    stale = command_reconciliation.find_stale_dispatched_commands(session, older_than_minutes=15)
    assert stale == []


def test_find_stale_dispatched_commands_skips_resolved_statuses(session):
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=30)
    _make_command(session, status="completed", created_at=old_ts)
    _make_command(session, status="failed", created_at=old_ts)
    _make_command(session, status="logged_only", created_at=old_ts)

    stale = command_reconciliation.find_stale_dispatched_commands(session, older_than_minutes=15)
    assert stale == []


def test_run_command_reconciliation_pass_marks_unknown_outcome_and_alerts(session, capsys):
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=30)
    cmd = _make_command(session, status="dispatched", created_at=old_ts, text="research XAUUSD news")

    reconciled = command_reconciliation.run_command_reconciliation_pass(session, older_than_minutes=15)

    assert len(reconciled) == 1
    assert reconciled[0].id == cmd.id
    assert reconciled[0].status == "unknown"
    assert "UNKNOWN_OUTCOME" in reconciled[0].result_text

    from core import ledger as ledger_module
    rows = session.query(ledger_module.LedgerEntry).filter_by(action="dashboard_command_result").all()
    assert len(rows) == 1
    assert "UNKNOWN_OUTCOME" in rows[0].result

    assert "RECONCILIATION ALERT" in capsys.readouterr().out


def test_mark_command_unknown_refuses_for_a_nonexistent_command(session):
    import pytest
    fake_stale = command_reconciliation.StaleCommand(command_request_id="does-not-exist", dispatched_at=datetime.now(timezone.utc))
    with pytest.raises(ValueError, match="No command_request"):
        command_reconciliation.mark_command_unknown(session, fake_stale)
