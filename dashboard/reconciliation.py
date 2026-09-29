"""
Reconciliation for CommandRequest rows stuck "dispatched" (Milestone 2's
Command box -> hermes-chief dispatch, dashboard/app.py::_dispatch_to_hermes_chief).

Mirrors core/reconciliation.py's EXECUTING-marker pattern for the same
class of problem: hermes-chief (or the Dashboard's own background task)
can always die mid-task, and pretending otherwise is how KNOWN_LIMITS bug
#11 happened in the first place - flagged as a real, not-yet-closed gap
in docs/evidence/milestone2_step5_command_box_dispatch.md, closed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core import ledger
from dashboard.models import CommandRequest


@dataclass(frozen=True)
class StaleCommand:
    command_request_id: str
    dispatched_at: datetime


def find_stale_dispatched_commands(session: Session, *, older_than_minutes: int, now: datetime | None = None) -> list[StaleCommand]:
    """A CommandRequest is stale if it's still "dispatched" (never reached
    "completed"/"failed") after older_than_minutes - _dispatch_to_hermes_chief
    always flips status to one of those on any outcome it can observe, so
    "still dispatched" past the window means the observer itself (hermes-
    chief, or the dispatching process) never got to report back."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=older_than_minutes)
    rows = session.query(CommandRequest).filter(CommandRequest.status == "dispatched").all()
    stale = []
    for row in rows:
        created_at = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
        if created_at <= cutoff:
            stale.append(StaleCommand(command_request_id=row.id, dispatched_at=created_at))
    return stale


def mark_command_unknown(session: Session, stale: StaleCommand, *, now: datetime | None = None) -> CommandRequest:
    """Deliberately does not guess completed/failed - same reasoning as
    core.reconciliation.mark_unknown_outcome: nobody knows, and assuming
    either is exactly the wrong move for something that may have already
    triggered a real Tier-2/3 approval."""
    now = now or datetime.now(timezone.utc)
    row = session.get(CommandRequest, stale.command_request_id)
    if row is None:
        raise ValueError(f"No command_request with id {stale.command_request_id}.")

    row.status = "unknown"
    row.result_text = (
        f"UNKNOWN_OUTCOME: dispatched at {stale.dispatched_at.isoformat()} but no result was ever recorded - "
        "hermes-chief (or the dispatching process) likely died mid-task. A human must check whether this "
        "command actually had any effect before trusting anything about it."
    )
    session.commit()

    ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=None, agent_role="owner", action="dashboard_command_result", tier=0,
            input_summary=row.text[:200], tool="hermes-chief",
            result=f"UNKNOWN_OUTCOME: {row.result_text}",
        ),
        ts=now,
    )
    # Interim alert channel (no real Telegram bot token - see
    # docs/KNOWN_LIMITS.md), same as core.reconciliation's own: a loud
    # stdout line the scheduler process's own logs pick up.
    print(
        f"[RECONCILIATION ALERT] command_request {row.id} has UNKNOWN_OUTCOME - a human must verify whether "
        "this command actually had any effect.",
        flush=True,
    )
    return row


def run_command_reconciliation_pass(session: Session, *, older_than_minutes: int, now: datetime | None = None) -> list[CommandRequest]:
    """The scheduler's automatic entry point (scripts/scheduler_loop.py),
    mirroring core.reconciliation.run_reconciliation_pass()'s own shape."""
    now = now or datetime.now(timezone.utc)
    stale = find_stale_dispatched_commands(session, older_than_minutes=older_than_minutes, now=now)
    return [mark_command_unknown(session, s, now=now) for s in stale]
