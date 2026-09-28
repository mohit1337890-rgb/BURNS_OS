import pytest

from core import policy_engine
from gateway import plugins, registry_bootstrap
from gateway.plugins.stubs import NotYetImplementedPlugin
from gateway.plugins.telegram import TelegramPlugin


REQUIRED_ENV = {
    "MAX_RISK_PER_TRADE_PCT": "1",
    "MAX_DAILY_LOSS_PCT": "3",
    "MONTHLY_AI_BUDGET_USD": "50",
    "DEFAULT_MISSION_BUDGET_USD": "10",
}


@pytest.fixture
def policy(monkeypatch):
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    return policy_engine.load_policy()


@pytest.fixture(autouse=True)
def clean_registry():
    plugins._REGISTRY.clear()
    yield
    plugins._REGISTRY.clear()


def test_enabled_plugin_with_env_present_registers_the_real_implementation(policy):
    env = {"TELEGRAM_BOT_TOKEN": "fake-token", "TELEGRAM_OWNER_CHAT_ID": "123"}
    readiness = registry_bootstrap.bootstrap(policy, env=env)

    assert readiness["send_message"].ready is True
    assert isinstance(plugins.get("send_message"), TelegramPlugin)


def test_enabled_plugin_missing_env_registers_a_refusing_stub(policy):
    env = {}  # TELEGRAM_BOT_TOKEN/TELEGRAM_OWNER_CHAT_ID both missing
    readiness = registry_bootstrap.bootstrap(policy, env=env)

    assert readiness["send_message"].ready is False
    stub = plugins.get("send_message")
    assert isinstance(stub, NotYetImplementedPlugin)
    result = stub.execute({"text": "hi"})
    assert result.ok is False
    assert "TELEGRAM_BOT_TOKEN" in result.detail


def test_disabled_plugin_registers_a_refusing_stub_even_with_all_env_present(policy):
    # place_trade is enabled: false in policy.yaml, regardless of env.
    env = {"MT5_LOGIN": "x", "MT5_PASSWORD": "y", "MT5_SERVER": "z"}
    readiness = registry_bootstrap.bootstrap(policy, env=env)

    assert readiness["place_trade"].ready is False
    assert "Disabled" in readiness["place_trade"].reason
    result = plugins.get("place_trade").execute({"symbol": "EURUSD"})
    assert result.ok is False


def test_every_policy_yaml_tier23_action_ends_up_registered(policy):
    # approve_mission_spec is deliberately excluded - see its note in
    # policy.yaml: it's never routed through gateway/plugins at all
    # (core/missions.py::request_spec_approval calls core.approvals
    # directly), so it has no plugin to register.
    registry_bootstrap.bootstrap(policy, env={})
    for action, rule in policy.actions.items():
        if rule.tier in (2, 3) and action != "approve_mission_spec":
            # Must not raise NotImplementedError - every tier 2/3 action
            # gets SOME registered plugin, even if it's a refusing stub.
            plugins.get(action)
