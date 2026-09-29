"""
Bearer-token auth for the Gateway's MCP streamable-http transport
(Milestone 2 - docs/MILESTONE_2_HERMES_DESIGN.md, "MCP transport"
section). Deliberately NOT the MCP SDK's own OAuth-resource-server
framework (mcp.server.auth) - that's built for a real external OAuth
issuer (RFC 8414/8707/9207 metadata, an issuer_url, etc.), which doesn't
fit two pre-shared, internally-issued bearer tokens. This is the same
"a plain shared-secret header check" shape gateway/app.py already uses
for GATEWAY_INTERNAL_TOKEN (X-API-Key) - consistent with the rest of this
codebase rather than introducing a second, heavier auth mechanism.

The whole point: `agent_role` for an MCP call must be determined from
WHICH bearer token authenticated the request, never from the tool call's
own JSON params - a compromised/hallucinating Hermes cannot claim to be
the other agent just by naming it. HermesBearerAuthMiddleware wraps the
ENTIRE streamable-http ASGI app (every request must pass through it - see
gateway/mcp_server.py::build_streamable_http_app) and either refuses with
401 before the app ever runs, or sets a request-scoped contextvar the
execute_action tool reads via get_current_agent_role().
"""

from __future__ import annotations

import contextvars
import os

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_current_agent_role: contextvars.ContextVar[str | None] = contextvars.ContextVar("hermes_agent_role", default=None)


def get_current_agent_role() -> str | None:
    """None on the stdio transport (this middleware never wraps it - see
    gateway/mcp_server.py::main) or if somehow called outside a request
    the middleware processed. execute_action() falls back to its own
    caller-supplied agent_role param in that case, preserving the
    stdio/pre-Milestone-2 trusted-caller behavior exactly."""
    return _current_agent_role.get()


def load_hermes_tokens_from_env() -> dict[str, str]:
    """token value -> agent_role. Both required (fail loudly, same
    philosophy as core/app_config.py) - a Gateway MCP endpoint running
    with an incomplete token set is a misconfiguration, not something to
    silently half-serve."""
    chief_token = os.environ.get("HERMES_MCP_TOKEN_CHIEF")
    researcher_token = os.environ.get("HERMES_MCP_TOKEN_RESEARCHER")
    missing = [
        name for name, val in [("HERMES_MCP_TOKEN_CHIEF", chief_token), ("HERMES_MCP_TOKEN_RESEARCHER", researcher_token)]
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"gateway.mcp_auth: environment variable(s) not set: {', '.join(missing)}. "
            "The Gateway MCP streamable-http endpoint refuses to start without both Hermes tokens configured."
        )
    if chief_token == researcher_token:
        raise RuntimeError(
            "gateway.mcp_auth: HERMES_MCP_TOKEN_CHIEF and HERMES_MCP_TOKEN_RESEARCHER must not be the same "
            "value - a shared token would make the two roles indistinguishable, defeating the whole split."
        )
    return {chief_token: "chief_of_staff", researcher_token: "researcher"}


class HermesBearerAuthMiddleware:
    """Plain ASGI middleware (not Starlette's BaseHTTPMiddleware, which
    buffers the whole response - the streamable-http transport uses
    long-lived streaming responses, so this wraps at the raw ASGI level
    instead, same as the MCP SDK's own AuthContextMiddleware does)."""

    def __init__(self, app: ASGIApp, tokens_by_value: dict[str, str]):
        self.app = app
        self._tokens_by_value = tokens_by_value

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        token = auth_header[len("Bearer "):] if auth_header.startswith("Bearer ") else None
        role = self._tokens_by_value.get(token) if token else None

        if role is None:
            response = JSONResponse({"error": "Missing or invalid bearer token."}, status_code=401)
            await response(scope, receive, send)
            return

        reset_token = _current_agent_role.set(role)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_agent_role.reset(reset_token)
