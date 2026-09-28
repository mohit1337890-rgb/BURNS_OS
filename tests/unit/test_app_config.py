import pytest

from core import app_config, policy_engine


FULL_CORE_ENV = {
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "burns_os",
    "BURNS_APP_DB_USER": "burns_app",  # the app's own restricted role - see KNOWN_LIMITS gap #10
    "LEDGER_ANCHOR_PATH": "./data/ledger_anchor.jsonl",
    "TELEGRAM_OWNER_CHAT_ID": "999888777",
    "MONTHLY_AI_BUDGET_USD": "50",
    "DEFAULT_MISSION_BUDGET_USD": "10",
    "MAX_RISK_PER_TRADE_PCT": "1",
    "MAX_DAILY_LOSS_PCT": "3",
}


def test_load_core_config_succeeds_with_all_vars_present():
    cfg = app_config.load_core_config(env=FULL_CORE_ENV)
    assert cfg.owner_chat_id == "999888777"
    assert cfg.monthly_ai_budget_usd == 50.0
    assert "burns_os" in cfg.database_url


def test_load_core_config_fails_loudly_listing_all_missing_vars():
    partial = {"POSTGRES_HOST": "localhost"}  # everything else missing
    with pytest.raises(app_config.ConfigError) as exc_info:
        app_config.load_core_config(env=partial)
    msg = str(exc_info.value)
    # Every missing var should be named, not just the first one found.
    assert "TELEGRAM_OWNER_CHAT_ID" in msg
    assert "MAX_RISK_PER_TRADE_PCT" in msg
    assert "MAX_DAILY_LOSS_PCT" in msg
    assert "LEDGER_ANCHOR_PATH" in msg


def test_load_core_config_fails_on_a_single_missing_var():
    env = dict(FULL_CORE_ENV)
    del env["MAX_RISK_PER_TRADE_PCT"]
    with pytest.raises(app_config.ConfigError, match="MAX_RISK_PER_TRADE_PCT"):
        app_config.load_core_config(env=env)


def test_load_core_config_allows_empty_app_db_password():
    env = dict(FULL_CORE_ENV)
    env["BURNS_APP_DB_PASSWORD"] = ""  # explicitly empty, not missing
    cfg = app_config.load_core_config(env=env)
    assert cfg.database_url == "postgresql+psycopg2://burns_app:@localhost:5432/burns_os"  # empty password segment, still constructs


def test_load_core_config_database_url_never_uses_the_admin_role(monkeypatch):
    """core.app_config.database_url must always be the restricted burns_app
    role, never POSTGRES_USER/PASSWORD (the admin/superuser - see
    db/migrations/env.py, which builds its own separate admin URL directly)
    - KNOWN_LIMITS gap #10. Setting POSTGRES_USER/PASSWORD in the env here
    and confirming they never appear in database_url is the regression
    guard against accidentally reintroducing that coupling."""
    env = dict(FULL_CORE_ENV)
    env["POSTGRES_USER"] = "burns_admin"
    env["POSTGRES_PASSWORD"] = "totally-different-admin-secret"
    cfg = app_config.load_core_config(env=env)
    assert "burns_admin" not in cfg.database_url
    assert "totally-different-admin-secret" not in cfg.database_url
    assert "burns_app" in cfg.database_url


def test_load_core_config_rejects_non_numeric_budget():
    env = dict(FULL_CORE_ENV)
    env["MONTHLY_AI_BUDGET_USD"] = "not-a-number"
    with pytest.raises(app_config.ConfigError, match="failed validation"):
        app_config.load_core_config(env=env)


# --- plugin readiness ----------------------------------------------------------

def test_disabled_plugin_is_never_ready_even_with_all_env_present():
    req = policy_engine.PluginRequirement(name="place_trade", enabled=False, required_env=("MT5_LOGIN",))
    readiness = app_config.check_plugin_ready(req, env={"MT5_LOGIN": "12345"})
    assert readiness.ready is False
    assert "Disabled" in readiness.reason


def test_enabled_plugin_missing_env_is_not_ready():
    req = policy_engine.PluginRequirement(
        name="send_email", enabled=True, required_env=("SMTP_HOST", "SMTP_PASSWORD"),
    )
    readiness = app_config.check_plugin_ready(req, env={"SMTP_HOST": "smtp.example.com"})
    assert readiness.ready is False
    assert "SMTP_PASSWORD" in readiness.missing_env


def test_enabled_plugin_with_all_env_present_is_ready():
    req = policy_engine.PluginRequirement(name="git_push", enabled=True, required_env=())
    readiness = app_config.check_plugin_ready(req, env={})
    assert readiness.ready is True


def test_policy_yaml_plugins_section_parses_into_requirements(monkeypatch):
    for k, v in {
        "MAX_RISK_PER_TRADE_PCT": "1", "MAX_DAILY_LOSS_PCT": "3",
        "MONTHLY_AI_BUDGET_USD": "50", "DEFAULT_MISSION_BUDGET_USD": "10",
    }.items():
        monkeypatch.setenv(k, v)
    policy = policy_engine.load_policy()
    assert "send_message" in policy.plugins
    assert policy.plugins["send_message"].enabled is True
    assert "TELEGRAM_BOT_TOKEN" in policy.plugins["send_message"].required_env
    assert policy.plugins["place_trade"].enabled is False
