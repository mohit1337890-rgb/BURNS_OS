import pytest

from core import budget_guard, ledger, llm_spend, policy_engine
from gateway import core_execute, plugins


REQUIRED_ENV = {
    "MAX_RISK_PER_TRADE_PCT": "1",
    "MAX_DAILY_LOSS_PCT": "3",
    "MONTHLY_AI_BUDGET_USD": "5",  # small on purpose, to make it easy to exceed
    "DEFAULT_MISSION_BUDGET_USD": "5",
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


def test_record_llm_spend_appears_in_ledger(session):
    entry = llm_spend.record_llm_spend(
        session, mission_id="m1", agent_role="researcher", model="claude-opus", cost_usd=0.75,
    )
    assert entry.tool == "claude-opus"
    assert entry.cost_usd == 0.75
    assert entry.action == "llm_call"


def test_record_llm_spend_rejects_negative_cost(session):
    with pytest.raises(ValueError):
        llm_spend.record_llm_spend(session, mission_id="m1", agent_role="researcher", model="x", cost_usd=-1.0)


def test_budget_guard_counts_llm_spend_toward_monthly_cap(session, policy):
    llm_spend.record_llm_spend(session, mission_id="m1", agent_role="researcher", model="claude-opus", cost_usd=4.0)
    status = budget_guard.check_monthly_budget(
        session, policy.monthly_budget_usd, policy.budget_warn_at_pct, policy.budget_stop_at_pct,
    )
    assert status.spend_usd == 4.0
    assert status.warn is True  # 4/5 = 80%, matches default warn_at_pct


def test_llm_spend_alone_can_trigger_a_budget_stop(session, policy):
    """The exact scenario the security review asked for: LLM spend ALONE
    (no plugin execution at all) can push the global monthly budget over
    its stop threshold and block a subsequent action."""
    llm_spend.record_llm_spend(session, mission_id="m1", agent_role="researcher", model="claude-opus", cost_usd=5.5)

    outcome = core_execute.request_action(
        session, policy, agent_role="researcher", mission_id="m1",
        action="web_search", params={}, input_summary="totally unrelated Tier 0 action",
    )
    assert outcome.status == "refused_budget"


def test_llm_spend_also_counts_toward_mission_budget(session, policy):
    llm_spend.record_llm_spend(session, mission_id="m1", agent_role="researcher", model="x", cost_usd=6.0)
    outcome = core_execute.request_action(
        session, policy, agent_role="researcher", mission_id="m1",
        action="web_search", params={}, input_summary="mission-scoped action",
    )
    assert outcome.status == "refused_budget"

    # A DIFFERENT mission's budget is untouched by mission "m1"'s spend.
    outcome2 = core_execute.request_action(
        session, policy, agent_role="researcher", mission_id="m2",
        action="web_search", params={}, input_summary="different mission entirely",
    )
    # Still refused - because m1's spend alone already blew the GLOBAL
    # monthly cap too (both are $5) - proves both caps are real, not just
    # mission-scoped.
    assert outcome2.status == "refused_budget"
