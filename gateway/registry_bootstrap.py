"""Registers every known plugin against its policy.yaml action name. Called
once at Gateway startup (and at the top of any test that needs the registry
populated) - see gateway/app.py.

Each plugin's readiness (core/app_config.py::check_plugin_ready) is checked
against policy.yaml's `plugins:` section BEFORE deciding what to register:
a disabled plugin, or one missing its own required env vars, gets a
NotYetImplementedPlugin with the SPECIFIC reason instead of the real
implementation - so it refuses to execute (closing KNOWN_LIMITS gap #9's
plugin half) without crashing the whole Gateway the way a hard failure at
import time would.
"""

from __future__ import annotations

import os

from core import app_config, policy_engine
from gateway.plugins import register
from gateway.plugins.email_smtp import SmtpEmailPlugin
from gateway.plugins.git_push import GitPushPlugin
from gateway.plugins.stubs import NotYetImplementedPlugin
from gateway.plugins.telegram import TelegramPlugin

_REAL_PLUGIN_FACTORIES = {
    "send_message": lambda policy: TelegramPlugin(),
    "send_email": lambda policy: SmtpEmailPlugin(),
    "git_push": lambda policy: GitPushPlugin(allowed_roots=policy.git_push_allowed_roots),
}

_UNIMPLEMENTED_TODOS = {
    "deploy_app": "Coolify integration not wired yet - needs a running Coolify instance.",
    "place_trade": "MT5 order execution not wired yet - needs its own risk-enforcement pass.",
    "publish_post": "No publishing target configured yet.",
    "create_invoice": "No invoicing backend configured yet.",
    "send_payment": "No payment provider configured yet.",
    "block_ip": "No firewall/WAF integration configured yet.",
    "delete_data": "No target data store configured yet.",
    "http_request_external": "Generic outbound HTTP not wired yet - needs a domain allowlist per call.",
    "deploy_app_production": "Production deploys not wired yet.",
}


def bootstrap(policy: policy_engine.PolicyDocument, env: dict | None = None) -> dict[str, "app_config.PluginReadiness"]:
    """Returns the readiness decision made for every plugin action named in
    policy.yaml's `plugins:` section, for callers (gateway/app.py's health
    endpoint) that want to report it rather than just silently act on it.
    """
    env = env if env is not None else os.environ
    readiness_report: dict[str, app_config.PluginReadiness] = {}

    for name, requirement in policy.plugins.items():
        readiness = app_config.check_plugin_ready(requirement, env=env)
        readiness_report[name] = readiness

        if readiness.ready and name in _REAL_PLUGIN_FACTORIES:
            register(name, _REAL_PLUGIN_FACTORIES[name](policy))
        else:
            register(name, NotYetImplementedPlugin(name, readiness.reason))

    # Actions with no policy.yaml `plugins:` entry at all (the always-stub
    # ones) still get a registered stub with their specific TODO, same as
    # before this function existed - registered_actions() must never come
    # back empty for a known policy.yaml action.
    for name, todo in _UNIMPLEMENTED_TODOS.items():
        if name not in policy.plugins:
            register(name, NotYetImplementedPlugin(name, todo))

    return readiness_report
