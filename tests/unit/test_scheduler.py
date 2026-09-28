from datetime import datetime, timedelta, timezone

import pytest

from core import approvals, ledger, policy_engine, scheduler
from gateway import plugins


REQUIRED_ENV = {
    "MAX_RISK_PER_TRADE_PCT": "1",
    "MAX_DAILY_LOSS_PCT": "3",
    "MONTHLY_AI_BUDGET_USD": "50",
    "DEFAULT_MISSION_BUDGET_USD": "10",
    "TELEGRAM_OWNER_CHAT_ID": "123",  # matches decide_approval(..., owner_chat_id="123") below
}


@pytest.fixture
def env(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    yield


@pytest.fixture
def policy(env):
    return policy_engine.load_policy()




@pytest.fixture(autouse=True)
def clean_registry():
    plugins._REGISTRY.clear()
    yield
    plugins._REGISTRY.clear()


class _FakePlugin:
    def __init__(self):
        self.calls = []

    def execute(self, params):
        self.calls.append(params)
        return plugins.PluginResult(ok=True, detail="done")


def _approved_tier3(session, policy):
    from gateway import core_execute
    plugins.register("place_trade", _FakePlugin())
    outcome = core_execute.request_action(
        session, policy, agent_role="risk-manager", mission_id="m1",
        action="place_trade", params={"symbol": "EURUSD"}, input_summary="demo buy",
    )
    approvals.decide_approval(
        session, outcome.approval_id, decided_by="Mohit", owner_chat_id="123", approve=True,
    )
    return outcome.approval_id


def test_find_due_tier3_approvals_excludes_still_cooling(session, policy):
    approval_id = _approved_tier3(session, policy)
    due = scheduler.find_due_tier3_approvals(session)
    assert due == []  # cooling period (10 min default) hasn't elapsed


def test_find_due_tier3_approvals_includes_elapsed_cooling(session, policy):
    approval_id = _approved_tier3(session, policy)
    future = datetime.now(timezone.utc) + timedelta(minutes=11)
    due = scheduler.find_due_tier3_approvals(session, now=future)
    assert len(due) == 1
    assert due[0].id == approval_id


def test_run_due_tier3_executions_actually_executes(session, policy):
    approval_id = _approved_tier3(session, policy)
    future = datetime.now(timezone.utc) + timedelta(minutes=11)

    outcomes = scheduler.run_due_tier3_executions(session, now=future)
    assert len(outcomes) == 1
    assert outcomes[0].status == "executed"

    req = approvals.get_approval(session, approval_id)
    assert req.status == approvals.ApprovalStatus.EXECUTED.value


def test_run_due_tier3_executions_is_idempotent_across_calls(session, policy):
    _approved_tier3(session, policy)
    future = datetime.now(timezone.utc) + timedelta(minutes=11)

    first_run = scheduler.run_due_tier3_executions(session, now=future)
    second_run = scheduler.run_due_tier3_executions(session, now=future)

    assert len(first_run) == 1
    assert first_run[0].status == "executed"
    assert len(second_run) == 0  # already EXECUTED, no longer APPROVED, so not "due" anymore


def test_expire_pending_approvals_flips_stale_pending_to_expired(session, policy):
    from gateway import core_execute
    plugins.register("send_message", _FakePlugin())
    outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "hi"}, input_summary="notify",
    )
    future = datetime.now(timezone.utc) + timedelta(hours=25)  # past the 24h default expiry

    count = scheduler.expire_pending_approvals(session, now=future)
    assert count == 1
    req = approvals.get_approval(session, outcome.approval_id, now=future)
    assert req.status == approvals.ApprovalStatus.EXPIRED.value


def test_expire_pending_approvals_leaves_fresh_pending_alone(session, policy):
    from gateway import core_execute
    plugins.register("send_message", _FakePlugin())
    outcome = core_execute.request_action(
        session, policy, agent_role="chief-of-staff", mission_id="m1",
        action="send_message", params={"text": "hi"}, input_summary="notify",
    )
    count = scheduler.expire_pending_approvals(session)
    assert count == 0
    req = approvals.get_approval(session, outcome.approval_id)
    assert req.status == approvals.ApprovalStatus.PENDING.value
