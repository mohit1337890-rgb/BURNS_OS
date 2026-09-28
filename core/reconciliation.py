"""
Reconciliation: recovering approvals left in an inconsistent state by a
process that died between claiming an execution (core.approvals.mark_executing,
which is deliberately irreversible - never auto-retried, since a Tier-3
action may have partially reached a real external system) and logging what
actually happened.

Two distinct mechanisms, for two distinct situations:

1. reconcile_stuck_approval() - a manual, explicit, audited admin action
   (scripts/admin_reconcile_approval.py) for an approval that predates the
   EXECUTING ledger marker entirely (gateway/core_execute.py) and so has NO
   marker for the automatic job below to find - e.g. approval
   d0521604-3749-4cfc-b3f5-0dafcc3681bf, left EXECUTED with zero ledger rows
   by a bug found live during STEP 3 testing (2026-09-29), fixed the same
   day. A human names the specific approval and the reason; this never runs
   on its own.

2. run_reconciliation_pass() - the automatic half, intended to be called
   periodically (scripts/scheduler_loop.py, same pattern as
   core/scheduler.py's own Tier-3 due-execution pass): finds every
   EXECUTING ledger marker with no later ledger row for the same
   approval_id, older than a configurable staleness window, and flips that
   approval to UNKNOWN_OUTCOME - a human must check whether the external
   action actually happened before trusting anything about that approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from core import ledger
from core.approvals import ApprovalRequest, ApprovalStatus
from core.ledger import LedgerEntry


def reconcile_stuck_approval(
    session: Session, approval_id: str, *, reason: str, reconciled_by: str, now: datetime | None = None,
) -> LedgerEntry:
    """Only for an approval stuck EXECUTED with no ledger record at all (the
    pre-EXECUTING-marker failure mode) - refuses anything else, since this
    is a manual override tool, not a general-purpose status editor. Does
    NOT touch any existing ledger row (append-only, never edited) - appends
    a new RECONCILED_FAILED entry that references the approval, and moves
    the approval itself to FAILED_RECONCILED so it can never again be
    mistaken for a normal successful EXECUTED action.
    """
    now = now or datetime.now(timezone.utc)
    req = session.query(ApprovalRequest).filter_by(id=approval_id).first()
    if req is None:
        raise ValueError(f"No approval request with id {approval_id}.")
    if req.status != ApprovalStatus.EXECUTED.value:
        raise ValueError(
            f"Approval {approval_id} is {req.status}, not EXECUTED - reconcile_stuck_approval() is only for an "
            "approval stuck EXECUTED with zero ledger record; anything else needs a different remedy."
        )
    # PENDING_APPROVAL is the normal, expected row every Tier-2/3 approval
    # gets at request_action() time (gateway/core_execute.py) - it exists
    # long before any execution attempt and doesn't count as "already has
    # an execution record" here. What this function is actually guarding
    # against is an EXECUTING marker or a final result (OK/FAILED/etc.)
    # already existing, which would mean the automatic reconciliation pass
    # (run_reconciliation_pass) is the right tool instead.
    existing = (
        session.query(LedgerEntry)
        .filter(LedgerEntry.approval_id == approval_id, LedgerEntry.result != "PENDING_APPROVAL")
        .count()
    )
    if existing > 0:
        raise ValueError(
            f"Approval {approval_id} already has {existing} execution-related ledger row(s) - "
            "reconcile_stuck_approval() is only for the zero-execution-rows case (pre-dates the EXECUTING marker); "
            "use the automatic reconciliation pass (run_reconciliation_pass) for an approval that DOES have one."
        )

    entry = ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=req.mission_id, agent_role=req.agent_role, action=req.action, tier=req.tier,
            input_summary=req.params_summary, tool=req.action,
            result=f"RECONCILED_FAILED: {reason} (reconciled by {reconciled_by})",
            approval_id=req.id, approved_by=req.decided_by,
        ),
        ts=now,
    )
    req.status = ApprovalStatus.FAILED_RECONCILED.value
    session.commit()
    return entry


@dataclass(frozen=True)
class StaleExecution:
    approval_id: str
    marker_entry_id: int
    marker_ts: datetime


def find_stale_executing_markers(session: Session, *, older_than_minutes: int, now: datetime | None = None) -> list[StaleExecution]:
    """An EXECUTING marker (gateway/core_execute.py::execute_approved_action)
    is stale if no LATER ledger row references the same approval_id - a
    later row (any result - OK/FAILED/etc.) means the same process finished
    normally and this marker is just historical, not stuck.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=older_than_minutes)

    markers = (
        session.query(LedgerEntry)
        .filter(LedgerEntry.result == "EXECUTING", LedgerEntry.approval_id.isnot(None))
        .order_by(LedgerEntry.id.asc())
        .all()
    )

    stale: list[StaleExecution] = []
    for marker in markers:
        marker_ts = marker.ts if marker.ts.tzinfo else marker.ts.replace(tzinfo=timezone.utc)
        if marker_ts > cutoff:
            continue  # too recent - still within its normal execution window
        later_exists = (
            session.query(LedgerEntry)
            .filter(LedgerEntry.approval_id == marker.approval_id, LedgerEntry.id > marker.id)
            .first()
            is not None
        )
        if not later_exists:
            stale.append(StaleExecution(approval_id=marker.approval_id, marker_entry_id=marker.id, marker_ts=marker_ts))
    return stale


def mark_unknown_outcome(session: Session, stale: StaleExecution, *, now: datetime | None = None) -> LedgerEntry:
    """Appends the UNKNOWN_OUTCOME ledger entry and flips the approval to
    ApprovalStatus.UNKNOWN_OUTCOME. Deliberately does not attempt to guess
    ok/failed - the whole point is that nobody knows, and pretending
    otherwise (auto-retrying, assuming failure) is exactly the wrong move
    for a Tier-2/3 action that might have already reached a real external
    system.
    """
    now = now or datetime.now(timezone.utc)
    req = session.query(ApprovalRequest).filter_by(id=stale.approval_id).first()
    if req is None:
        raise ValueError(f"No approval request with id {stale.approval_id} (referenced by ledger entry {stale.marker_entry_id}).")

    entry = ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=req.mission_id, agent_role=req.agent_role, action=req.action, tier=req.tier,
            input_summary=req.params_summary, tool=req.action,
            result=(
                f"UNKNOWN_OUTCOME: EXECUTING marker (ledger id={stale.marker_entry_id}, "
                f"written {stale.marker_ts.isoformat()}) had no result after the reconciliation window - "
                "the process likely died mid-execution. A human must check whether the external action "
                "actually happened before trusting anything about this approval."
            ),
            approval_id=req.id, approved_by=req.decided_by,
        ),
        ts=now,
    )
    req.status = ApprovalStatus.UNKNOWN_OUTCOME.value
    session.commit()
    # ALERT: real delivery is Telegram (core.ledger_anchor.TelegramAnchorSink's
    # sibling would be a similar owner-alert sink) - not live yet (no real
    # bot token, see docs/KNOWN_LIMITS.md), so this is the interim channel:
    # a loud, impossible-to-miss stdout line the scheduler's own process
    # logs pick up (docker compose logs scheduler).
    print(
        f"[RECONCILIATION ALERT] approval {req.id} ({req.action}, tier {req.tier}) has UNKNOWN_OUTCOME - "
        f"a human must verify whether this action actually ran. Ledger entry {entry.id}.",
        flush=True,
    )
    return entry


def run_reconciliation_pass(session: Session, *, older_than_minutes: int, now: datetime | None = None) -> list[LedgerEntry]:
    """The scheduler's automatic entry point (scripts/scheduler_loop.py),
    mirroring core.scheduler.run_due_tier3_executions()'s own shape."""
    now = now or datetime.now(timezone.utc)
    stale = find_stale_executing_markers(session, older_than_minutes=older_than_minutes, now=now)
    return [mark_unknown_outcome(session, s, now=now) for s in stale]
