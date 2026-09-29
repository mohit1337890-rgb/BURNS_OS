import pytest

from core import approvals, ledger, missions, policy_engine
from core.approval_channels import TelegramChannel

TELEGRAM_CHANNEL = TelegramChannel()

REQUIRED_ENV = {
    "MAX_RISK_PER_TRADE_PCT": "1",
    "MAX_DAILY_LOSS_PCT": "3",
    "MONTHLY_AI_BUDGET_USD": "50",
    "DEFAULT_MISSION_BUDGET_USD": "10",
    "TELEGRAM_OWNER_CHAT_ID": "owner-chat",  # matches _approve_spec()'s decide_approval call below
}


@pytest.fixture
def env(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    yield


@pytest.fixture
def policy(env):
    return policy_engine.load_policy()




def _valid_spec(**overrides) -> dict:
    spec = {
        "goal": "Build a small TODO web app",
        "users": ["Mohit"],
        "scope_in": ["add task", "complete task"],
        "scope_out": ["multi-user auth"],
        "features": ["add", "complete", "delete"],
        "stack": "FastAPI + HTMX",
        "risks": ["none major"],
        "legal_checks": ["n/a - internal tool"],
        "acceptance_tests": ["can add a task and see it in the list"],
        "budget_usd": 5.0,
        "squad": ["backend-dev", "qa-tester"],
    }
    spec.update(overrides)
    return spec


def _approve_spec(session, mission):
    """Test helper standing in for the real Approvals Bot flow (a human
    pressing Approve on Telegram) - calls the same core.approvals.decide_approval
    the real bot calls."""
    return approvals.decide_approval(
        session, mission.spec_approval_id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=True, chat_id="owner-chat",
    )


def test_create_mission_starts_in_intake(session):
    m = missions.create_mission(session, mission_id="mis-1", title="TODO app")
    assert m.status == missions.MissionStatus.INTAKE.value


def test_set_spec_moves_to_spec_draft(session):
    m = missions.create_mission(session, mission_id="mis-1", title="TODO app")
    m = missions.set_spec(session, m, _valid_spec())
    assert m.status == missions.MissionStatus.SPEC_DRAFT.value
    assert m.budget_usd == 5.0


def test_validate_spec_lists_all_missing_fields(session):
    incomplete = {"goal": "x"}
    with pytest.raises(missions.InvalidSpecError) as exc_info:
        missions.validate_spec(incomplete)
    msg = str(exc_info.value)
    assert "users" in msg
    assert "acceptance_tests" in msg


def test_awaiting_approval_and_running_are_refused_via_generic_transition(session, policy):
    # Both gated targets must go through their dedicated functions - the
    # generic transition() refuses them outright, even from a state where
    # they'd otherwise be structurally allowed.
    m = missions.create_mission(session, mission_id="mis-1", title="TODO app")
    m = missions.set_spec(session, m, _valid_spec())
    with pytest.raises(missions.MissionError, match="request_spec_approval"):
        missions.transition(session, m, missions.MissionStatus.AWAITING_APPROVAL)

    m = missions.request_spec_approval(session, policy, m)
    _approve_spec(session, m)
    with pytest.raises(missions.MissionError, match="start_running"):
        missions.transition(session, m, missions.MissionStatus.RUNNING)


def test_request_spec_approval_creates_a_real_pending_approval(session, policy):
    m = missions.create_mission(session, mission_id="mis-1", title="TODO app")
    m = missions.set_spec(session, m, _valid_spec())
    m = missions.request_spec_approval(session, policy, m)

    assert m.status == missions.MissionStatus.AWAITING_APPROVAL.value
    assert m.spec_approval_id is not None
    req = approvals.get_approval(session, m.spec_approval_id)
    assert req.status == approvals.ApprovalStatus.PENDING.value
    assert req.tier == 2


def test_request_spec_approval_requires_complete_spec(session, policy):
    m = missions.create_mission(session, mission_id="mis-1", title="TODO app")
    m = missions.set_spec(session, m, {"goal": "x"})  # incomplete
    with pytest.raises(missions.InvalidSpecError):
        missions.request_spec_approval(session, policy, m)


def test_cannot_start_running_without_a_real_owner_approval(session, policy):
    """The core security-relevant guarantee: a mission can NEVER reach
    RUNNING unless a real ApprovalRequest for its spec exists AND is
    APPROVED - not just because its status string happens to say
    AWAITING_APPROVAL."""
    m = missions.create_mission(session, mission_id="mis-1", title="TODO app")
    m = missions.set_spec(session, m, _valid_spec())
    m = missions.request_spec_approval(session, policy, m)
    assert m.status == missions.MissionStatus.AWAITING_APPROVAL.value

    # Approval is still PENDING - nobody has approved it yet.
    with pytest.raises(missions.MissionError, match="not APPROVED"):
        missions.start_running(session, m)

    # Explicitly REJECTED - must still refuse.
    approvals.decide_approval(
        session, m.spec_approval_id, decided_by="Mohit", channel=TELEGRAM_CHANNEL, approve=False, chat_id="owner-chat",
    )
    with pytest.raises(missions.MissionError, match="not APPROVED"):
        missions.start_running(session, m)


def test_start_running_fails_closed_if_approval_link_is_missing(session, policy):
    # Simulates a corrupted/bypassed mission record (e.g. a direct DB
    # write) where spec_approval_id was never set - must fail closed, not
    # silently allow RUNNING just because the status string matches.
    m = missions.create_mission(session, mission_id="mis-1", title="TODO app")
    m = missions.set_spec(session, m, _valid_spec())
    m = missions.request_spec_approval(session, policy, m)
    m.spec_approval_id = None
    session.commit()
    with pytest.raises(missions.MissionError, match="no linked spec approval"):
        missions.start_running(session, m)


def test_start_running_succeeds_after_real_approval(session, policy):
    m = missions.create_mission(session, mission_id="mis-1", title="TODO app")
    m = missions.set_spec(session, m, _valid_spec())
    m = missions.request_spec_approval(session, policy, m)
    _approve_spec(session, m)

    m = missions.start_running(session, m)
    assert m.status == missions.MissionStatus.RUNNING.value


def test_full_happy_path(session, policy):
    m = missions.create_mission(session, mission_id="mis-1", title="TODO app")
    m = missions.set_spec(session, m, _valid_spec())
    m = missions.request_spec_approval(session, policy, m)
    _approve_spec(session, m)
    m = missions.start_running(session, m)
    assert m.status == missions.MissionStatus.RUNNING.value

    m = missions.transition(session, m, missions.MissionStatus.QUALITY_GATES)
    m = missions.transition(session, m, missions.MissionStatus.DELIVERY_REVIEW)
    m = missions.transition(session, m, missions.MissionStatus.DEPLOYING)
    m = missions.transition(session, m, missions.MissionStatus.DONE)
    assert m.status == missions.MissionStatus.DONE.value


def test_cannot_skip_states(session, policy):
    m = missions.create_mission(session, mission_id="mis-1", title="TODO app")
    m = missions.set_spec(session, m, _valid_spec())
    # Still in SPEC_DRAFT - jumping straight to QUALITY_GATES must be refused.
    with pytest.raises(missions.InvalidTransitionError) as exc_info:
        missions.transition(session, m, missions.MissionStatus.QUALITY_GATES)
    assert exc_info.value.current == "SPEC_DRAFT"
    assert exc_info.value.target == "QUALITY_GATES"


def test_cannot_transition_out_of_terminal_state(session, policy):
    m = missions.create_mission(session, mission_id="mis-1", title="TODO app")
    m = missions.set_spec(session, m, _valid_spec())
    m = missions.request_spec_approval(session, policy, m)
    m = missions.transition(session, m, missions.MissionStatus.CANCELLED)
    assert m.status == missions.MissionStatus.CANCELLED.value

    with pytest.raises(missions.MissionError):
        missions.start_running(session, m)


def test_failed_transition_records_reason(session, policy):
    m = missions.create_mission(session, mission_id="mis-1", title="TODO app")
    m = missions.set_spec(session, m, _valid_spec())
    m = missions.request_spec_approval(session, policy, m)
    _approve_spec(session, m)
    m = missions.start_running(session, m)
    m = missions.transition(session, m, missions.MissionStatus.FAILED, failure_reason="build failed: missing dependency")
    assert m.status == missions.MissionStatus.FAILED.value
    assert "missing dependency" in m.failure_reason


def test_quality_gate_results_requires_all_applicable_gates():
    ok = missions.QualityGateResults(
        build_passed=True, tests_passed=True, playwright_passed=True,
        security_gate_passed=True, docs_present=True,
    )
    assert ok.all_required_passed is True

    missing_docs = missions.QualityGateResults(
        build_passed=True, tests_passed=True, playwright_passed=True,
        security_gate_passed=True, docs_present=False,
    )
    assert missing_docs.all_required_passed is False


def test_quality_gate_results_ignores_playwright_when_not_applicable():
    no_ui_mission = missions.QualityGateResults(
        build_passed=True, tests_passed=True, playwright_passed=None,
        security_gate_passed=True, docs_present=True,
    )
    assert no_ui_mission.all_required_passed is True
