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
from sqlalchemy.orm import sessionmaker

from core import app_config, approvals, ledger, ledger_anchor, policy_engine
from gateway import core_execute


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
    def execute_action(agent_role: str, action: str, input_summary: str, mission_id: str | None = None, params: dict | None = None) -> dict:
        session = session_factory()
        try:
            outcome = core_execute.request_action(
                session, policy, agent_role=agent_role, mission_id=mission_id,
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
    real-env config gateway/app.py's lifespan uses, then runs the server
    over stdio (MCPServer.run()'s default transport). stdio means the trust
    boundary is "whoever can spawn this process" (a future Hermes container
    launching it as a subprocess) rather than a network port - so unlike
    gateway/app.py's HTTP /execute, there is no separate API-key check here
    by design; nothing else listens on a socket.
    """
    core_config = app_config.load_core_config()
    engine = ledger.get_engine(core_config.database_url)
    session_factory = ledger.get_session_factory(engine)
    policy = policy_engine.load_policy()

    sinks = [ledger_anchor.FileAnchorSink(core_config.anchor_path)]
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        sinks.append(ledger_anchor.TelegramAnchorSink())

    server = build_server(session_factory, policy, anchor_sinks=sinks)
    server.run()


if __name__ == "__main__":
    main()
