"""
Mission Manager (BUILD PROMPT section 4.5). A Mission moves through a fixed
state machine - INTAKE -> SPEC_DRAFT -> AWAITING_APPROVAL -> RUNNING ->
QUALITY_GATES -> DELIVERY_REVIEW -> DEPLOYING -> DONE, with FAILED/
CANCELLED reachable from most states. transition() is the ONLY way a
mission's status changes - it validates the move against
_ALLOWED_TRANSITIONS before touching the DB, so an illegal jump (e.g.
INTAKE straight to DEPLOYING, skipping the owner's spec approval) raises
instead of silently happening. This is the same "enforced in code, not
prompts" principle as core/policy_engine.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import JSON, Column, DateTime, Float, String
from sqlalchemy.orm import Session

from core import approvals, policy_engine
from core.ledger import Base

REQUIRED_SPEC_FIELDS = (
    "goal", "users", "scope_in", "scope_out", "features", "stack", "risks",
    "legal_checks", "acceptance_tests", "budget_usd", "squad",
)


class MissionStatus(str, Enum):
    INTAKE = "INTAKE"
    SPEC_DRAFT = "SPEC_DRAFT"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RUNNING = "RUNNING"
    QUALITY_GATES = "QUALITY_GATES"
    DELIVERY_REVIEW = "DELIVERY_REVIEW"
    DEPLOYING = "DEPLOYING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# Every status not listed here as a key has no allowed outgoing transitions
# (DONE/FAILED/CANCELLED are terminal).
_ALLOWED_TRANSITIONS: dict[MissionStatus, tuple[MissionStatus, ...]] = {
    MissionStatus.INTAKE: (MissionStatus.SPEC_DRAFT, MissionStatus.CANCELLED),
    MissionStatus.SPEC_DRAFT: (MissionStatus.AWAITING_APPROVAL, MissionStatus.CANCELLED),
    MissionStatus.AWAITING_APPROVAL: (MissionStatus.RUNNING, MissionStatus.CANCELLED, MissionStatus.SPEC_DRAFT),
    MissionStatus.RUNNING: (MissionStatus.QUALITY_GATES, MissionStatus.FAILED, MissionStatus.CANCELLED),
    MissionStatus.QUALITY_GATES: (MissionStatus.DELIVERY_REVIEW, MissionStatus.RUNNING, MissionStatus.FAILED),
    MissionStatus.DELIVERY_REVIEW: (MissionStatus.DEPLOYING, MissionStatus.DONE, MissionStatus.RUNNING, MissionStatus.FAILED),
    MissionStatus.DEPLOYING: (MissionStatus.DONE, MissionStatus.FAILED),
}


class MissionError(Exception):
    pass


class InvalidTransitionError(MissionError):
    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(f"Cannot transition mission from {current} to {target} - not an allowed move.")


class InvalidSpecError(MissionError):
    pass


class Mission(Base):
    __tablename__ = "missions"

    id = Column(String, primary_key=True)
    title = Column(String, nullable=False)
    status = Column(String, nullable=False, default=MissionStatus.INTAKE.value)
    spec = Column(JSON, nullable=True)
    budget_usd = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    failure_reason = Column(String, nullable=True)
    # Set only by request_spec_approval() below - the real ApprovalRequest
    # id the owner must approve via Telegram before start_running() will
    # allow RUNNING. This is what makes "AWAITING_APPROVAL" a real gate
    # instead of just a status label - see start_running()'s check.
    spec_approval_id = Column(String, nullable=True)


def create_mission(session: Session, *, mission_id: str, title: str, now: datetime | None = None) -> Mission:
    now = now or datetime.now(timezone.utc)
    m = Mission(
        id=mission_id, title=title, status=MissionStatus.INTAKE.value,
        spec=None, budget_usd=0.0, created_at=now, updated_at=now,
    )
    session.add(m)
    session.commit()
    session.refresh(m)
    return m


def validate_spec(spec: dict) -> None:
    """Spec-Kit structure check (BUILD PROMPT 4.5: "goal, users, scope
    in/out, features, stack, risks, legal checks, acceptance tests, budget,
    squad"). Raises InvalidSpecError listing every missing field at once
    (not just the first one found) so a caller can fix a draft spec in one
    pass instead of a frustrating field-at-a-time loop.
    """
    missing = [f for f in REQUIRED_SPEC_FIELDS if f not in spec or spec[f] in (None, "", [])]
    if missing:
        raise InvalidSpecError(f"Spec is missing required field(s): {', '.join(missing)}.")
    if not isinstance(spec.get("acceptance_tests"), list) or len(spec["acceptance_tests"]) == 0:
        raise InvalidSpecError("Spec must have at least one acceptance_tests entry.")


# These two targets are deliberately EXCLUDED from transition() below and
# have their own dedicated functions instead (request_spec_approval,
# start_running) - a generic status-name check ("are we in
# AWAITING_APPROVAL?") is not the same guarantee as "does a REAL,
# owner-decided ApprovalRequest exist for this exact spec?". An earlier
# version of this module only did the former, which meant nothing actually
# stopped a caller from moving AWAITING_APPROVAL -> RUNNING without the
# owner ever having approved anything - see
# test_cannot_start_running_without_a_real_owner_approval.
_GATED_TARGETS = (MissionStatus.AWAITING_APPROVAL, MissionStatus.RUNNING)


def transition(session: Session, mission: Mission, target: MissionStatus, *,
                failure_reason: str | None = None, now: datetime | None = None) -> Mission:
    if target in _GATED_TARGETS:
        raise MissionError(
            f"{target.value} cannot be reached via transition() - use "
            f"{'request_spec_approval()' if target == MissionStatus.AWAITING_APPROVAL else 'start_running()'} instead, "
            "which enforces the real approval check this generic function cannot."
        )
    current = MissionStatus(mission.status)
    allowed = _ALLOWED_TRANSITIONS.get(current, ())
    if target not in allowed:
        raise InvalidTransitionError(current.value, target.value)

    mission.status = target.value
    mission.updated_at = now or datetime.now(timezone.utc)
    if target in (MissionStatus.FAILED, MissionStatus.CANCELLED) and failure_reason:
        mission.failure_reason = failure_reason
    session.commit()
    session.refresh(mission)
    return mission


def request_spec_approval(
    session: Session, policy: policy_engine.PolicyDocument, mission: Mission, *, now: datetime | None = None,
) -> Mission:
    """SPEC_DRAFT -> AWAITING_APPROVAL. Creates a REAL Tier-2 ApprovalRequest
    (core/approvals.py) that must be approved by the owner via the
    Approvals Bot before start_running() will allow RUNNING - see that
    function's check against mission.spec_approval_id.
    """
    current = MissionStatus(mission.status)
    allowed = _ALLOWED_TRANSITIONS.get(current, ())
    if MissionStatus.AWAITING_APPROVAL not in allowed:
        raise InvalidTransitionError(current.value, MissionStatus.AWAITING_APPROVAL.value)
    if not mission.spec:
        raise InvalidSpecError("Cannot request spec approval without a spec attached (see set_spec()).")
    validate_spec(mission.spec)

    req = approvals.create_approval(
        session, mission_id=mission.id, agent_role="chief-of-staff", action="approve_mission_spec",
        tier=2, params_summary=f"Approve spec for mission '{mission.title}'", params={"spec": mission.spec},
        expire_after_hours=policy.approval_expire_hours, cooling_minutes=0, now=now,
    )
    mission.spec_approval_id = req.id
    mission.status = MissionStatus.AWAITING_APPROVAL.value
    mission.updated_at = now or datetime.now(timezone.utc)
    session.commit()
    session.refresh(mission)
    return mission


def start_running(session: Session, mission: Mission, *, now: datetime | None = None) -> Mission:
    """AWAITING_APPROVAL -> RUNNING - ONLY if mission.spec_approval_id
    points to a real ApprovalRequest whose status is APPROVED. This is the
    actual enforcement that a mission can never start running on the
    owner's behalf without the owner having actually approved its spec -
    see test_cannot_start_running_without_a_real_owner_approval.
    """
    current = MissionStatus(mission.status)
    allowed = _ALLOWED_TRANSITIONS.get(current, ())
    if MissionStatus.RUNNING not in allowed:
        raise InvalidTransitionError(current.value, MissionStatus.RUNNING.value)

    if not mission.spec_approval_id:
        raise MissionError("Mission has no linked spec approval - call request_spec_approval() first.")
    req = approvals.get_approval(session, mission.spec_approval_id, now=now)
    if req is None:
        raise MissionError(f"Linked approval {mission.spec_approval_id} does not exist.")
    if req.status != approvals.ApprovalStatus.APPROVED.value:
        raise MissionError(
            f"Cannot start running: spec approval is {req.status}, not APPROVED. "
            "The owner must approve the mission spec via the Approvals Bot first."
        )

    mission.status = MissionStatus.RUNNING.value
    mission.updated_at = now or datetime.now(timezone.utc)
    session.commit()
    session.refresh(mission)
    return mission


def set_spec(session: Session, mission: Mission, spec: dict) -> Mission:
    """Attaches/updates the spec while still in INTAKE or SPEC_DRAFT -
    does NOT itself validate the full Spec-Kit requirement (a
    work-in-progress draft is allowed to be incomplete); validate_spec() is
    only enforced at the SPEC_DRAFT -> AWAITING_APPROVAL transition, so the
    owner is the one who signs off on a complete spec, not this function.
    """
    current = MissionStatus(mission.status)
    if current not in (MissionStatus.INTAKE, MissionStatus.SPEC_DRAFT):
        raise MissionError(f"Cannot set spec while mission is {current.value}.")
    mission.spec = spec
    mission.budget_usd = float(spec.get("budget_usd", mission.budget_usd) or mission.budget_usd)
    mission.status = MissionStatus.SPEC_DRAFT.value
    mission.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(mission)
    return mission


@dataclass(frozen=True)
class QualityGateResults:
    build_passed: bool
    tests_passed: bool
    playwright_passed: bool | None  # None = not applicable (no UI in this mission)
    security_gate_passed: bool
    docs_present: bool

    @property
    def all_required_passed(self) -> bool:
        gates = [self.build_passed, self.tests_passed, self.security_gate_passed, self.docs_present]
        if self.playwright_passed is not None:
            gates.append(self.playwright_passed)
        return all(gates)
