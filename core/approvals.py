"""
Approvals (BUILD PROMPT section 4.4 backend). A Tier 2/3 action never
executes directly - the Gateway creates an ApprovalRequest row instead and
returns "pending", and only main.plugins execution after decide_approval()
marks it APPROVED does the Gateway actually run the tool. The Approvals Bot
(a separate module, not built yet - see docs/KNOWN_LIMITS.md) is just a
Telegram-facing client of create_approval()/decide_approval() below; all the
actual state-machine logic and rules (owner-only, exact-scope, 24h expiry,
Tier-3 cooling period) live here so they're enforced regardless of which
front-end calls them.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import JSON, Column, DateTime, Integer, String, update
from sqlalchemy.orm import Session

from core.ledger import Base


def compute_params_hash(params: dict) -> str:
    """Canonical SHA-256 of the approval's params, computed once at
    create_approval() and re-checked at execution time
    (gateway/core_execute.py::execute_approved_action) - if the stored
    params dict has been mutated since (a direct DB write, a future bug),
    the hash won't match and execution is refused rather than silently
    running whatever params happen to be there now. Fixes KNOWN_LIMITS
    gap #4 (approvals not bound to exact params).
    """
    return hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"
    # Set only by core.reconciliation.reconcile_stuck_approval() (an
    # explicit, audited admin action) - an approval that mark_executing()
    # already flipped to EXECUTED before the process died with no ledger
    # record of what (if anything) actually happened. Distinct from
    # EXECUTED so it's never mistaken for a normal successful run.
    FAILED_RECONCILED = "FAILED_RECONCILED"
    # Set only by core.reconciliation.mark_unknown_outcome() - an approval
    # whose EXECUTING ledger marker (gateway/core_execute.py) had no
    # matching result after the reconciliation window elapsed. A human
    # must check whether the external action actually happened.
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


class ApprovalRequest(Base):
    __tablename__ = "approvals"

    id = Column(String, primary_key=True)
    mission_id = Column(String, nullable=True)
    agent_role = Column(String, nullable=False)
    action = Column(String, nullable=False)
    tier = Column(Integer, nullable=False)
    params_summary = Column(String, nullable=False)
    params = Column(JSON, nullable=False, default=dict)
    params_hash = Column(String(64), nullable=False)
    requested_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String, nullable=False, default=ApprovalStatus.PENDING.value)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decided_by = Column(String, nullable=True)
    # Tier 3 only: execution is refused until now() >= cooling_until, even
    # after APPROVED - see is_executable().
    cooling_until = Column(DateTime(timezone=True), nullable=True)


class ApprovalError(Exception):
    pass


class NotOwnerError(ApprovalError):
    """Raised when anyone other than the configured owner chat tries to
    decide an approval - the Approvals Bot must check this at its own
    layer too (only reading messages from TELEGRAM_OWNER_CHAT_ID at all),
    but this is the enforcement layer that can't be bypassed by a
    misconfigured or compromised bot front-end."""


def create_approval(
    session: Session, *, mission_id: str | None, agent_role: str, action: str, tier: int,
    params_summary: str, params: dict, expire_after_hours: int, cooling_minutes: int,
    now: datetime | None = None,
) -> ApprovalRequest:
    if tier not in (2, 3):
        raise ApprovalError(f"Only Tier 2/3 actions need an approval request; got tier={tier}.")
    now = now or datetime.now(timezone.utc)
    req = ApprovalRequest(
        id=str(uuid.uuid4()),
        mission_id=mission_id,
        agent_role=agent_role,
        action=action,
        tier=tier,
        params_summary=params_summary,
        params=params,
        params_hash=compute_params_hash(params),
        requested_at=now,
        expires_at=now + timedelta(hours=expire_after_hours),
        status=ApprovalStatus.PENDING.value,
        cooling_until=(now + timedelta(minutes=cooling_minutes)) if tier == 3 else None,
    )
    session.add(req)
    session.commit()
    session.refresh(req)
    return req


def _expire_if_needed(req: ApprovalRequest, now: datetime) -> ApprovalRequest:
    expires_at = req.expires_at if req.expires_at.tzinfo else req.expires_at.replace(tzinfo=timezone.utc)
    if req.status == ApprovalStatus.PENDING.value and now > expires_at:
        req.status = ApprovalStatus.EXPIRED.value
    return req


def get_approval(session: Session, approval_id: str, now: datetime | None = None) -> ApprovalRequest | None:
    now = now or datetime.now(timezone.utc)
    req = session.query(ApprovalRequest).filter_by(id=approval_id).first()
    if req is None:
        return None
    req = _expire_if_needed(req, now)
    session.commit()
    return req


def decide_approval(
    session: Session, approval_id: str, *, decided_by: str, owner_chat_id: str, approve: bool,
    now: datetime | None = None,
) -> ApprovalRequest:
    """decided_by/owner_chat_id are checked separately so a caller can't
    accidentally decide on the owner's behalf just by passing a matching
    name string - owner_chat_id must equal the configured
    TELEGRAM_OWNER_CHAT_ID, checked by the caller (Approvals Bot) and
    re-asserted here as a second, independent gate.

    This second gate was previously a no-op - the parameter was accepted
    but never compared against anything, so NotOwnerError could never
    actually be raised from here (only approvals_bot.bot.handle_callback_query's
    own from_chat_id check protected a decision, a single layer despite
    this docstring's claim of two). Found and fixed 2026-09-29 during live
    bring-up testing.
    """
    now = now or datetime.now(timezone.utc)
    configured_owner = os.environ.get("TELEGRAM_OWNER_CHAT_ID", "")
    if not configured_owner or str(owner_chat_id) != configured_owner:
        raise NotOwnerError(
            f"owner_chat_id {owner_chat_id!r} does not match the configured TELEGRAM_OWNER_CHAT_ID - refusing to decide approval {approval_id}."
        )
    req = get_approval(session, approval_id, now)
    if req is None:
        raise ApprovalError(f"No approval request with id {approval_id}.")
    if req.status != ApprovalStatus.PENDING.value:
        raise ApprovalError(f"Approval {approval_id} is already {req.status}, cannot decide again.")

    req.status = ApprovalStatus.APPROVED.value if approve else ApprovalStatus.REJECTED.value
    req.decided_at = now
    req.decided_by = decided_by
    session.commit()
    session.refresh(req)
    return req


def is_executable(req: ApprovalRequest, now: datetime | None = None) -> tuple[bool, str]:
    """Gateway calls this right before actually running the tool for a
    previously-approved Tier 2/3 action. Returns (ok, reason). Checking
    req.status here alone is NOT a sufficient idempotency guard on its own
    (a read-then-act race between two concurrent calls) - see
    mark_executing() below, which is the actual atomic guard.
    """
    now = now or datetime.now(timezone.utc)
    if req.status != ApprovalStatus.APPROVED.value:
        return False, f"Approval status is {req.status}, not APPROVED."
    if req.tier == 3 and req.cooling_until is not None:
        cooling_until = req.cooling_until if req.cooling_until.tzinfo else req.cooling_until.replace(tzinfo=timezone.utc)
        if now < cooling_until:
            remaining = (cooling_until - now).total_seconds()
            return False, f"Tier 3 cooling period not elapsed yet ({remaining:.0f}s remaining)."
    return True, "ok"


def verify_params_unchanged(req: ApprovalRequest) -> bool:
    """True if req.params still hashes to what was stored at
    create_approval() time - fixes KNOWN_LIMITS gap #4. A mismatch means
    the stored params were mutated after the owner approved (whatever they
    approved is no longer what would run), and execution must be refused
    rather than silently running the current (unapproved) params.
    """
    return compute_params_hash(req.params or {}) == req.params_hash


def mark_executing(session: Session, approval_id: str) -> bool:
    """Atomically claims the right to execute this approval, exactly once.

    Uses a single WHERE-gated UPDATE (APPROVED -> EXECUTED) rather than a
    separate read-then-write, so that if this is called twice - a
    double-tapped Telegram button, a replayed webhook delivery - only the
    FIRST caller's UPDATE actually matches a row (rowcount=1, returns
    True); the second sees the row already at EXECUTED (rowcount=0,
    returns False) with no race window between the two calls. This closes
    KNOWN_LIMITS gap #5 (no idempotency guard) - is_executable() alone only
    inspects the current status, which a concurrent second call could read
    before the first call's write lands.
    """
    result = session.execute(
        update(ApprovalRequest)
        .where(ApprovalRequest.id == approval_id, ApprovalRequest.status == ApprovalStatus.APPROVED.value)
        .values(status=ApprovalStatus.EXECUTED.value)
    )
    session.commit()
    return result.rowcount == 1
