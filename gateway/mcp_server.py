"""
Gateway MCP server (BUILD PROMPT STEP 3 / section 4.1's "Hermes can use it
later"): exposes the exact same core_execute actions the HTTP Gateway
(gateway/app.py) does, as MCP tools, so a future Hermes Agent container can
call the Gateway without ever holding a real credential itself - it only
ever sees action names + params, same chokepoint principle as the REST
API.

build_server() takes an explicit session_factory + policy (dependency
injection, same pattern as gateway/app.py's configure_*() functions) so
tests can point this at an in-memory SQLite engine - see
tests/unit/test_mcp_server.py, which calls list_tools()/call_tool()
directly rather than going over a real stdio/SSE transport (transport
itself isn't exercised here, same as gateway/app.py's tests use
TestClient rather than a real running HTTP server).
"""

from __future__ import annotations

import json
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.orm import sessionmaker

from core import app_config, approvals, ledger, ledger_anchor, policy_engine
from gateway import core_execute, mcp_auth, registry_bootstrap


def build_server(session_factory: sessionmaker, policy: policy_engine.PolicyDocument, anchor_sinks: list | None = None) -> MCPServer:
    anchor_sinks = anchor_sinks or []
    server = MCPServer(
        name="burns-gateway",
        instructions=(
            "Burns OS Gateway - the ONLY way to take an external/Tier-2/3 action. "
            "Tier 0/1 actions execute immediately; Tier 2/3 actions create a pending "
            "approval the owner must decide on via the Approvals Bot before anything "
            "actually happens externally."
        ),
    )

    @server.tool(description="Request an action be taken. Tier 0/1 executes immediately (logged); Tier 2/3 creates a pending approval and does NOT execute yet.")
    def execute_action(action: str, input_summary: str, agent_role: str | None = None, mission_id: str | None = None, params: dict | None = None) -> dict:
        # Milestone 2: over the authenticated streamable-http transport,
        # HermesBearerAuthMiddleware has already set this contextvar from
        # WHICH bearer token authenticated the request - that always wins
        # over whatever this call's own JSON params claim agent_role is
        # (never trust the caller for its own identity). On the stdio
        # transport (no middleware wraps it - see main() below), the
        # contextvar is never set, so this falls back to the caller-
        # supplied value exactly as every pre-Milestone-2 stdio caller
        # already relied on (process-spawn trust, unchanged).
        resolved_role = mcp_auth.get_current_agent_role() or agent_role
        if not resolved_role:
            return {"status": "refused", "detail": "agent_role is required (missing from both the authenticated token and the call's own params)."}
        session = session_factory()
        try:
            outcome = core_execute.request_action(
                session, policy, agent_role=resolved_role, mission_id=mission_id,
                action=action, params=params or {}, input_summary=input_summary,
            )
            return {
                "status": outcome.status, "detail": outcome.detail,
                "ledger_entry_id": outcome.ledger_entry_id, "approval_id": outcome.approval_id,
            }
        finally:
            session.close()

    @server.tool(description="Check the status of a previously-requested Tier 2/3 approval.")
    def get_approval_status(approval_id: str) -> dict:
        session = session_factory()
        try:
            req = approvals.get_approval(session, approval_id)
            if req is None:
                return {"error": f"No approval with id {approval_id}."}
            return {
                "id": req.id, "status": req.status, "tier": req.tier, "action": req.action,
                "agent_role": req.agent_role, "mission_id": req.mission_id,
                "expires_at": req.expires_at.isoformat(),
                "cooling_until": req.cooling_until.isoformat() if req.cooling_until else None,
            }
        finally:
            session.close()

    @server.tool(description="Verify the Ledger's hash chain and external tail-truncation anchors are both intact.")
    def verify_ledger() -> dict:
        session = session_factory()
        try:
            chain_result = ledger.verify_chain(session)
            anchor_result = ledger_anchor.verify_against_anchors(session, anchor_sinks)
            return {
                "chain_ok": chain_result.ok, "chain_total_entries": chain_result.total_entries,
                "chain_reason": chain_result.reason,
                "anchor_ok": anchor_result.ok, "anchor_checked": anchor_result.checked_anchors,
                "anchor_reason": anchor_result.reason,
            }
        finally:
            session.close()

    return server


def build_streamable_http_app(session_factory: sessionmaker, policy: policy_engine.PolicyDocument, anchor_sinks: list | None = None):
    """Milestone 2: the SAME build_server() output as stdio, over
    streamable-http, wrapped in HermesBearerAuthMiddleware so every
    request must present a valid HERMES_MCP_TOKEN_CHIEF/_RESEARCHER
    bearer token before it ever reaches a tool - a request with no/wrong
    token gets a 401 from the middleware and never runs execute_action at
    all (see gateway/mcp_auth.py's own docstring for why this is a plain
    header check, not the MCP SDK's OAuth-resource-server framework)."""
    server = build_server(session_factory, policy, anchor_sinks=anchor_sinks)
    tokens_by_value = mcp_auth.load_hermes_tokens_from_env()
    port = os.environ.get("MCP_HTTP_PORT", "8091")
    # DNS-rebinding protection (the SDK's own, on by default) checks the
    # Host header against an allowlist - the default only covers
    # 127.0.0.1/localhost, which doesn't include this service's real
    # docker-compose hostname. Explicit, not disabled - callers still must
    # present one of these exact Host values.
    security = TransportSecuritySettings(
        allowed_hosts=[f"gateway-mcp:{port}", "gateway-mcp", f"localhost:{port}", f"127.0.0.1:{port}"],
    )
    inner_app = server.streamable_http_app(transport_security=security)
    return mcp_auth.HermesBearerAuthMiddleware(inner_app, tokens_by_value)


def _tool_result_to_dict(result) -> dict:
    """Test/debug helper: MCP's CallToolResult wraps content blocks (text,
    possibly structured) - this unwraps the common case (our tools all
    return a single JSON-serializable dict) back into a plain Python dict
    for assertions, without callers needing to know MCP's content-block
    format."""
    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    for block in result.content:
        if hasattr(block, "text"):
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                continue
    raise ValueError(f"Could not extract a dict from tool result: {result!r}")


def main() -> None:
    """Real entry point (`python -m gateway.mcp_server`) - wires the same
    real-env config gateway/app.py's lifespan uses.

    Two transports, chosen by MCP_TRANSPORT (default "stdio"):
      - stdio: MCPServer.run()'s default. Trust boundary is "whoever can
        spawn this process" - no separate API-key check by design; nothing
        else listens on a socket. Used by tests spawning this as a real
        subprocess.
      - streamable-http: Milestone 2's Gateway-MCP service (docker-compose
        `gateway-mcp`) - HermesBearerAuthMiddleware-wrapped, listens on
        MCP_HTTP_PORT (default 8091), NOT published to the host (internal
        Docker network only - see docs/MILESTONE_2_HERMES_DESIGN.md).
    """
    core_config = app_config.load_core_config()
    engine = ledger.get_engine(core_config.database_url)
    session_factory = ledger.get_session_factory(engine)
    policy = policy_engine.load_policy()

    sinks = [ledger_anchor.FileAnchorSink(core_config.anchor_path)]
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        sinks.append(ledger_anchor.TelegramAnchorSink())

    # This process has its own separate gateway.plugins._REGISTRY (a fresh
    # module-level dict - gateway-mcp is a different container/process
    # from gateway, they never share in-memory state) - without this, the
    # Tier 0/1 real plugins Milestone 2 needs (web_fetch,
    # submit_research_report, get_research_report - see
    # gateway/core_execute.py's Tier 0/1 branch) would never be registered
    # here and would silently fall back to "EXECUTED_IN_SANDBOX" with no
    # actual effect.
    registry_bootstrap.bootstrap(policy, session_factory=session_factory)

    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        import uvicorn

        app = build_streamable_http_app(session_factory, policy, anchor_sinks=sinks)
        port = int(os.environ.get("MCP_HTTP_PORT", "8091"))
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    elif transport == "stdio":
        server = build_server(session_factory, policy, anchor_sinks=sinks)
        server.run()
    else:
        raise ValueError(f"Unknown MCP_TRANSPORT {transport!r} - must be 'stdio' or 'streamable-http'.")


if __name__ == "__main__":
    main()
