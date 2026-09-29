import os

import pytest

from core import policy_engine


REQUIRED_ENV = {
    "MAX_RISK_PER_TRADE_PCT": "1",
    "MAX_DAILY_LOSS_PCT": "3",
    "MONTHLY_AI_BUDGET_USD": "50",
    "DEFAULT_MISSION_BUDGET_USD": "10",
}


@pytest.fixture
def env(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    yield


def test_load_policy_succeeds_with_env_set(env):
    policy = policy_engine.load_policy()
    assert policy.max_risk_per_trade_pct == 1.0
    assert policy.max_daily_loss_pct == 3.0
    assert policy.monthly_budget_usd == 50.0
    assert policy.default_mission_budget_usd == 10.0


def test_load_policy_fails_loudly_on_missing_env_var(monkeypatch):
    # Deliberately leave MAX_RISK_PER_TRADE_PCT unset - a risk limit
    # silently defaulting to empty/zero is exactly the failure mode this
    # guards against.
    monkeypatch.delenv("MAX_RISK_PER_TRADE_PCT", raising=False)
    monkeypatch.setenv("MAX_DAILY_LOSS_PCT", "3")
    monkeypatch.setenv("MONTHLY_AI_BUDGET_USD", "50")
    monkeypatch.setenv("DEFAULT_MISSION_BUDGET_USD", "10")
    with pytest.raises(policy_engine.PolicyError, match="MAX_RISK_PER_TRADE_PCT"):
        policy_engine.load_policy()


def test_tier0_action_classifies_as_tier0(env):
    policy = policy_engine.load_policy()
    verdict = policy_engine.classify(policy, "web_search")
    assert verdict.tier == 0
    assert verdict.is_unknown_action is False


def test_tier3_trading_action_classifies_as_tier3(env):
    policy = policy_engine.load_policy()
    verdict = policy_engine.classify(policy, "place_trade")
    assert verdict.tier == 3


def test_unknown_action_defaults_to_tier3(env):
    policy = policy_engine.load_policy()
    verdict = policy_engine.classify(policy, "some_action_nobody_defined")
    assert verdict.tier == 3
    assert verdict.is_unknown_action is True


def test_hard_blocked_action_raises(env):
    policy = policy_engine.load_policy()
    with pytest.raises(policy_engine.HardBlockedError):
        policy_engine.classify(policy, "reveal_secrets")


@pytest.mark.parametrize("action", [
    "reveal_secrets", "modify_policies", "modify_permissions", "disable_logging",
    "act_outside_mission_scope", "delete_ledger", "impersonate_owner",
])
def test_every_documented_hard_block_action_actually_raises(env, action):
    """Closes a gap found during the 2026-09-29 security-claims audit: only
    2 of policy.yaml's 7 hard_block entries (reveal_secrets,
    modify_policies) had a test asserting classify() actually raises for
    them - the other 5 were only ever covered implicitly by being in the
    same YAML list, with nothing to catch a future edit silently dropping
    one. classify()'s logic is identical for every entry, but the DATA
    (which actions are on the list) is exactly the kind of thing a
    refactor/edit could silently break without this."""
    policy = policy_engine.load_policy()
    assert action in policy.hard_block
    with pytest.raises(policy_engine.HardBlockedError):
        policy_engine.classify(policy, action)


def test_hard_block_cannot_be_bypassed_by_any_tier_logic(env):
    """Regression guard: hard_block must be checked BEFORE the actions
    dict lookup, regardless of whether the action also happens to appear
    in policy.actions with some tier assigned."""
    policy = policy_engine.load_policy()
    assert "modify_policies" in policy.hard_block
    with pytest.raises(policy_engine.HardBlockedError):
        policy_engine.classify(policy, "modify_policies")


def test_load_policy_scope_file_points_to_a_real_existing_file(env):
    """Regression test: a prior version of policy.yaml's security.scope_file
    value ("policies/authorised_scopes.yaml") double-counted the
    "policies/" directory segment (load_policy resolves it relative to
    policy.yaml's OWN directory, which is already policies/), silently
    resolving to a nonexistent policies/policies/authorised_scopes.yaml.
    Nothing caught this before because every other test passed an explicit
    `path` override to is_target_authorised() instead of using the
    policy's own loaded scope_file - this test exercises that real path
    end to end."""
    policy = policy_engine.load_policy()
    assert policy.scope_file.exists(), (
        f"policy.scope_file resolved to {policy.scope_file}, which doesn't exist - "
        "check policy.yaml's security.scope_file value and how load_policy() resolves it."
    )


def test_authorised_scope_empty_by_default(env, tmp_path):
    scopes_file = tmp_path / "authorised_scopes.yaml"
    scopes_file.write_text("scopes: []\n", encoding="utf-8")
    assert policy_engine.is_target_authorised("anything.example.com", path=scopes_file) is False


def test_researcher_role_is_capped_at_tier0(env):
    policy = policy_engine.load_policy()
    verdict = policy_engine.classify(policy, "web_search")  # tier 0
    policy_engine.check_role_limit(policy, "researcher", verdict)  # must not raise


def test_researcher_role_refused_above_tier0(env):
    policy = policy_engine.load_policy()
    verdict = policy_engine.classify(policy, "send_email")  # tier 2
    with pytest.raises(policy_engine.RoleNotAllowedError, match="researcher"):
        policy_engine.check_role_limit(policy, "researcher", verdict)


def test_chief_of_staff_role_forbidden_from_web_tools(env):
    policy = policy_engine.load_policy()
    for action in ("web_search", "web_fetch"):
        verdict = policy_engine.classify(policy, action)
        with pytest.raises(policy_engine.RoleNotAllowedError, match="forbidden_actions"):
            policy_engine.check_role_limit(policy, "chief_of_staff", verdict)


def test_chief_of_staff_role_allowed_up_to_tier3(env):
    policy = policy_engine.load_policy()
    verdict = policy_engine.classify(policy, "place_trade")  # tier 3
    policy_engine.check_role_limit(policy, "chief_of_staff", verdict)  # must not raise


def test_unlisted_agent_role_is_unrestricted(env):
    """Backward compatibility: every pre-Milestone-2 caller (Dashboard,
    scheduler, approvals_bot, and every ad-hoc agent_role string already
    used elsewhere in this test suite) has no entry in policy.roles and
    must be completely unaffected by this check."""
    policy = policy_engine.load_policy()
    for action in ("place_trade", "web_search", "send_email"):
        verdict = policy_engine.classify(policy, action)
        policy_engine.check_role_limit(policy, "sales-researcher", verdict)  # must not raise
        policy_engine.check_role_limit(policy, "owner", verdict)  # must not raise


def test_authorised_scope_true_only_when_owner_confirmed(env, tmp_path):
    scopes_file = tmp_path / "authorised_scopes.yaml"
    scopes_file.write_text(
        "scopes:\n"
        "  - target: approved.example.com\n"
        "    owner_confirmed: true\n"
        "  - target: not-confirmed.example.com\n"
        "    owner_confirmed: false\n",
        encoding="utf-8",
    )
    assert policy_engine.is_target_authorised("approved.example.com", path=scopes_file) is True
    assert policy_engine.is_target_authorised("not-confirmed.example.com", path=scopes_file) is False
    assert policy_engine.is_target_authorised("never-listed.example.com", path=scopes_file) is False
