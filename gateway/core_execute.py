"""
The Gateway's actual decision/execution logic (BUILD PROMPT section 4.1),
kept as plain functions with no FastAPI dependency so it's directly
unit-testable - gateway/app.py is a thin HTTP wrapper around this module.

Flow for every call:
  1. classify tier (policy_engine.classify) - hard-blocked actions refuse
     immediately and are still logged (result=REFUSED_HARD_BLOCK).
  2. check budget (budget_guard.enforce) - over-budget refuses immediately,
     also logged (result=REFUSED_BUDGET).
  3. Tier 0/1: these are in-sandbox/read-only actions the agent runtime
     executes directly - the Gateway's only job is to record it in the
     Ledger for audit completeness. No plugin/credential involved.
  4. Tier 2/3: the Gateway does NOT execute yet - it creates an
     ApprovalRequest and logs a PENDING entry. The actual plugin call only
     happens later, from execute_approved_action(), once a human has
     approved it (and, for Tier 3, the cooling period has elapsed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from core import approvals, budget_guard, dlp, ledger, policy_engine
from gateway import plugins


@dataclass(frozen=True)
class ActionOutcome:
    status: str  # "executed" | "pending_approval" | "refused_hard_block" | "refused_budget"
    detail: str
    ledger_entry_id: Optional[int] = None
    approval_id: Optional[str] = None


def request_action(
    session: Session,
    policy: policy_engine.PolicyDocument,
    *,
    agent_role: str,
    mission_id: Optional[str],
    action: str,
    params: dict,
    input_summary: str,
) -> ActionOutcome:
    # Step 1: tier classification, including the hard-block check.
    try:
        verdict = policy_engine.classify(policy, action)
    except policy_engine.HardBlockedError as exc:
        entry = ledger.append_entry(
            session,
            ledger.LedgerEntryInput(
                mission_id=mission_id, agent_role=agent_role, action=action, tier=-1,
                input_summary=input_summary, tool=action, result="REFUSED_HARD_BLOCK",
            ),
        )
        return ActionOutcome(status="refused_hard_block", detail=str(exc), ledger_entry_id=entry.id)

    # Step 2: budget check (even Tier 0/1 actions consume LLM tokens).
    try:
        budget_guard.enforce(
            session, mission_id,
            monthly_cap_usd=policy.monthly_budget_usd,
            mission_cap_usd=policy.default_mission_budget_usd,
            warn_at_pct=policy.budget_warn_at_pct,
            stop_at_pct=policy.budget_stop_at_pct,
        )
    except budget_guard.BudgetExceededError as exc:
        entry = ledger.append_entry(
            session,
            ledger.LedgerEntryInput(
                mission_id=mission_id, agent_role=agent_role, action=action, tier=verdict.tier,
                input_summary=input_summary, tool=action, result="REFUSED_BUDGET",
            ),
        )
        return ActionOutcome(status="refused_budget", detail=str(exc), ledger_entry_id=entry.id)

    # Step 3: Tier 0/1 - executes in-sandbox, Gateway just logs it.
    if verdict.tier in (0, 1):
        entry = ledger.append_entry(
            session,
            ledger.LedgerEntryInput(
                mission_id=mission_id, agent_role=agent_role, action=action, tier=verdict.tier,
                input_summary=input_summary, tool=action, result="EXECUTED_IN_SANDBOX",
            ),
        )
        return ActionOutcome(status="executed", detail="Tier 0/1 action executed in-sandbox.", ledger_entry_id=entry.id)

    # Step 4: Tier 2/3 - create an approval request, do NOT execute yet.
    req = approvals.create_approval(
        session, mission_id=mission_id, agent_role=agent_role, action=action, tier=verdict.tier,
        params_summary=input_summary, params=params,
        expire_after_hours=policy.approval_expire_hours,
        cooling_minutes=policy.tier3_cooling_minutes,
    )
    entry = ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=mission_id, agent_role=agent_role, action=action, tier=verdict.tier,
            input_summary=input_summary, tool=action, result="PENDING_APPROVAL",
            approval_id=req.id,
        ),
    )
    return ActionOutcome(
        status="pending_approval",
        detail=f"Tier {verdict.tier} action requires approval (id={req.id}).",
        ledger_entry_id=entry.id, approval_id=req.id,
    )


def _refuse_not_executed(session: Session, req, reason: str) -> "ActionOutcome":
    entry = ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=req.mission_id, agent_role=req.agent_role, action=req.action, tier=req.tier,
            input_summary=req.params_summary, tool=req.action, result=f"NOT_EXECUTED: {reason}",
            approval_id=req.id, approved_by=req.decided_by,
        ),
    )
    return ActionOutcome(status="not_executed", detail=reason, ledger_entry_id=entry.id, approval_id=req.id)


def execute_approved_action(session: Session, approval_id: str, now: Optional[datetime] = None) -> ActionOutcome:
    """Called after a human has decided on a Tier 2/3 approval request
    (core/approvals.py::decide_approval). Only actually runs the plugin
    (and only then touches a real external system / spends real cost) if:
      - the approval is APPROVED and, for Tier 3, its cooling period has
        elapsed (approvals.is_executable);
      - the stored params still hash to what was approved
        (approvals.verify_params_unchanged - KNOWN_LIMITS gap #4);
      - this call WINS the atomic claim on execution
        (approvals.mark_executing - KNOWN_LIMITS gap #5): a second call for
        the same approval_id (double-tap, replayed webhook) sees the row
        already claimed and is refused, never running the plugin twice.

    `now` defaults to real wall-clock time - core/scheduler.py passes its
    own `now` through explicitly so a scheduler pass and the execution it
    triggers agree on what time it is (a real bug caught by
    test_scheduler.py: without this, the scheduler could decide an item was
    due using a test-simulated future time while this function's own
    default `datetime.now()` still saw the real, non-elapsed cooling
    period, and refused to execute the very item the scheduler just found).
    """
    req = approvals.get_approval(session, approval_id, now=now)
    if req is None:
        raise approvals.ApprovalError(f"No approval request with id {approval_id}.")

    ok, reason = approvals.is_executable(req, now=now)
    if not ok:
        return _refuse_not_executed(session, req, reason)

    if not approvals.verify_params_unchanged(req):
        return _refuse_not_executed(
            session, req,
            "Stored params no longer match what was approved (hash mismatch) - a new approval is required.",
        )

    dlp_findings = dlp.scan_params(req.params or {})
    if dlp_findings:
        labels = ", ".join(sorted({f.label for f in dlp_findings}))
        # Deliberately does NOT call mark_executing - a DLP refusal doesn't
        # consume the one-time execution claim, since nothing was sent.
        return _refuse_not_executed(
            session, req,
            f"Outgoing content DLP check found possible secret(s) ({labels}) - refused, owner alerted.",
        )

    if not approvals.mark_executing(session, approval_id):
        # Someone else's call already claimed this approval - this is the
        # idempotency guard actually firing, not an error state.
        return _refuse_not_executed(session, req, "Already executed (or claimed by a concurrent call) - not running again.")

    # mark_executing() has now irreversibly flipped this approval to
    # EXECUTED (by design - see its own docstring: never auto-retried,
    # since a Tier-3 action may have partially reached a real external
    # system even if what follows here blows up). Everything from here on
    # is a place where the process could die before a result is ever
    # logged - a silent, permanent, unrecorded action, violating this
    # system's one non-negotiable rule ("every action is logged,
    # permanently" - see the append-only trigger's own error message).
    #
    # Two independent layers close this, because they catch different
    # failure classes:
    #  1. This EXECUTING marker, written and COMMITTED before the plugin is
    #     even called - if the process is killed outright (os._exit, OOM
    #     kill, power loss - anything that skips Python's own exception
    #     handling entirely), this row is the only trace that an execution
    #     was ever attempted. core.reconciliation's scheduler job finds an
    #     EXECUTING marker with no matching result row after N minutes and
    #     flips it to UNKNOWN_OUTCOME for a human to check.
    #  2. The try/except below, for the much more common case of an
    #     ordinary Python exception (an unregistered plugin, a plugin bug,
    #     a network error) - this still lets the SAME process log a
    #     specific FAILED reason immediately, rather than waiting for the
    #     reconciliation job's timeout.
    # Found live during STEP 3 test C (2026-09-29): an unregistered plugin
    # (a bug in the caller, not this function) raised NotImplementedError
    # from plugins.get() and left a silent, unrecorded EXECUTED approval
    # behind - closed by layer 2. Layer 1 (this marker) is what a genuine
    # process kill mid-plugin-call needs, which no try/except can catch.
    ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=req.mission_id, agent_role=req.agent_role, action=req.action, tier=req.tier,
            input_summary=req.params_summary, tool=req.action,
            result="EXECUTING",
            approval_id=req.id, approved_by=req.decided_by,
        ),
    )

    try:
        plugin = plugins.get(req.action)
        result = plugin.execute(req.params or {})
    except Exception as exc:  # noqa: BLE001 - deliberately broad: ANY failure here must still be logged, never silently lost
        entry = ledger.append_entry(
            session,
            ledger.LedgerEntryInput(
                mission_id=req.mission_id, agent_role=req.agent_role, action=req.action, tier=req.tier,
                input_summary=req.params_summary, tool=req.action,
                result=f"FAILED: unhandled exception during execution - {exc!r}",
                approval_id=req.id, approved_by=req.decided_by,
            ),
        )
        return ActionOutcome(
            status="execution_failed", detail=f"Unhandled exception during execution: {exc}",
            ledger_entry_id=entry.id, approval_id=req.id,
        )

    entry = ledger.append_entry(
        session,
        ledger.LedgerEntryInput(
            mission_id=req.mission_id, agent_role=req.agent_role, action=req.action, tier=req.tier,
            input_summary=req.params_summary, tool=req.action,
            result=("OK: " + result.detail) if result.ok else ("FAILED: " + result.detail),
            approval_id=req.id, approved_by=req.decided_by,
            cost_usd=result.cost_usd, evidence_links=result.evidence_links or [],
        ),
    )
    return ActionOutcome(
        status="executed" if result.ok else "execution_failed",
        detail=result.detail, ledger_entry_id=entry.id, approval_id=req.id,
    )
