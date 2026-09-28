"""
Explicit stub plugins - registered so the action doesn't fail with
NotImplementedError, but every one returns ok=False with a clear TODO
reason instead of silently pretending to succeed. Each has a real,
dedicated implementation plan noted below rather than being left vague.

TODO deploy_app (Coolify): call Coolify's REST API
(POST /api/v1/applications/{uuid}/deploy) with a Coolify API token read
from a new COOLIFY_API_TOKEN/.env var - needs a running Coolify instance
first (part of docker-compose.yml, not yet stood up - see
docs/KNOWN_LIMITS.md, Docker isn't installed on this dev machine).

TODO place_trade (MT5 demo order): reuse the MetaTrader5 execution pattern
already built and battle-tested in the sibling "trading ai" project
(C:\\Users\\91813\\Desktop\\trading ai\\execution.py) - real order placement,
with MAX_RISK_PER_TRADE_PCT/MAX_DAILY_LOSS_PCT enforced in code BEFORE the
order reaches MT5 (not just checked in policy.yaml), matching what
risk_engine.py already does there. This needs its own focused pass since
getting the risk-limit enforcement right is safety-critical, not something
to rush through as a stub-to-real conversion.

TODO publish_post / create_invoice / send_payment / block_ip / delete_data /
http_request_external: no owner-provided credentials/target service exists
yet for any of these (no CRM/invoicing/firewall/generic-HTTP target has
been decided) - implement once a real target is chosen.
"""

from __future__ import annotations

from gateway.plugins import PluginResult


class NotYetImplementedPlugin:
    def __init__(self, action: str, todo: str):
        self._action = action
        self._todo = todo

    def execute(self, params: dict) -> PluginResult:
        return PluginResult(
            ok=False,
            detail=f"'{self._action}' is not implemented yet - {self._todo}",
        )
