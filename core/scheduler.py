"""
Scheduler (BUILD PROMPT section 4.5-adjacent; closes KNOWN_LIMITS gap #6).

The cooling period itself was already enforced (gateway.core_execute won't
run a Tier-3 action before cooling_until), but nothing automatically
retried it once the cooling period elapsed - a human or process had to call
execute_approved_action again by hand. run_due_tier3_executions() is that
process: intended to be called periodically (a real cron/loop is part of
Milestone STEP 3, not this module - this is the pure logic, directly
testable with a fake "now").

Also proactively expires PENDING approvals past their expires_at, rather
than relying only on the lazy expire-on-read in core.approvals.get_approval
- a mission stuck in AWAITING_APPROVAL with an approval nobody ever looked
at again should still visibly flip to EXPIRED once a scheduler pass runs.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from core import approvals
from core.approvals import ApprovalRequest, ApprovalStatus
from gateway import core_execute


def find_due_tier3_approvals(session: Session, now: datetime | None = None) -> list[ApprovalRequest]:
    """APPROVED Tier-3 approvals whose cooling period has elapsed and that
    haven't been executed yet - the scheduler's actual worklist."""
    now = now or datetime.now(timezone.utc)
    candidates = (
        session.query(ApprovalRequest)
        .filter(ApprovalRequest.tier == 3, ApprovalRequest.status == ApprovalStatus.APPROVED.value)
        .all()
    )
    due = []
    for req in candidates:
        ok, _ = approvals.is_executable(req, now=now)
        if ok:
            due.append(req)
    return due


def expire_pending_approvals(session: Session, now: datetime | None = None) -> int:
    """Proactively flips any PENDING approval past its expires_at to
    EXPIRED. Returns how many were expired. Safe to call as often as the
    scheduler loop runs - a no-op when nothing is due.
    """
    now = now or datetime.now(timezone.utc)
    pending = session.query(ApprovalRequest).filter(ApprovalRequest.status == ApprovalStatus.PENDING.value).all()
    count = 0
    for req in pending:
        before = req.status
        approvals._expire_if_needed(req, now)  # noqa: SLF001 - same module family, not a real external boundary
        if req.status != before:
            count += 1
    if count:
        session.commit()
    return count


def run_due_tier3_executions(session: Session, now: datetime | None = None) -> list[core_execute.ActionOutcome]:
    """The scheduler's main entry point: expires stale pending approvals,
    then executes every Tier-3 approval whose cooling period has elapsed.
    Each execution still goes through execute_approved_action's full guard
    chain (is_executable, params-hash check, atomic mark_executing) - this
    function only decides WHICH approvals are due, never bypasses those
    checks.
    """
    now = now or datetime.now(timezone.utc)
    expire_pending_approvals(session, now)
    outcomes = []
    for req in find_due_tier3_approvals(session, now):
        outcomes.append(core_execute.execute_approved_action(session, req.id, now=now))
    return outcomes
