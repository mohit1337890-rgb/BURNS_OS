import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from core import ledger, policy_engine
from gateway import mcp_auth, mcp_server, plugins

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


@pytest.fixture
def policy(env):
    return policy_engine.load_policy()


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    ledger.init_db(engine)
    return ledger.get_session_factory(engine)


@pytest.fixture
def server(session_factory, policy):
    return mcp_server.build_server(session_factory, policy)


@pytest.fixture(autouse=True)
def clean_registry():
    plugins._REGISTRY.clear()
    yield
    plugins._REGISTRY.clear()


class _FakePlugin:
    def execute(self, params):
        return plugins.PluginResult(ok=True, detail="done")


@pytest.mark.asyncio
async def test_lists_all_three_tools(server):
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {"execute_action", "get_approval_status", "verify_ledger"}


@pytest.mark.asyncio
async def test_execute_action_tool_tier0(server):
    result = await server.call_tool("execute_action", {
        "agent_role": "researcher", "action": "web_search", "input_summary": "test",
    })
    body = mcp_server._tool_result_to_dict(result)
    assert body["status"] == "executed"


@pytest.mark.asyncio
async def test_execute_action_tool_tier2_creates_approval(server):
    plugins.register("send_message", _FakePlugin())
    result = await server.call_tool("execute_action", {
        "agent_role": "chief-of-staff", "action": "send_message",
        "input_summary": "notify", "params": {"text": "hi"},
    })
    body = mcp_server._tool_result_to_dict(result)
    assert body["status"] == "pending_approval"
    assert body["approval_id"] is not None


@pytest.mark.asyncio
async def test_get_approval_status_tool(server):
    plugins.register("send_message", _FakePlugin())
    exec_result = await server.call_tool("execute_action", {
        "agent_role": "chief-of-staff", "action": "send_message",
        "input_summary": "notify", "params": {"text": "hi"},
    })
    approval_id = mcp_server._tool_result_to_dict(exec_result)["approval_id"]

    status_result = await server.call_tool("get_approval_status", {"approval_id": approval_id})
    body = mcp_server._tool_result_to_dict(status_result)
    assert body["status"] == "PENDING"


@pytest.mark.asyncio
async def test_get_approval_status_tool_unknown_id(server):
    result = await server.call_tool("get_approval_status", {"approval_id": "does-not-exist"})
    body = mcp_server._tool_result_to_dict(result)
    assert "error" in body


@pytest.mark.asyncio
async def test_verify_ledger_tool_on_fresh_db(server):
    result = await server.call_tool("verify_ledger", {})
    body = mcp_server._tool_result_to_dict(result)
    assert body["chain_ok"] is True
    assert body["anchor_ok"] is True


@pytest.mark.asyncio
async def test_execute_action_uses_contextvar_role_over_params_role(server):
    """Milestone 2: simulates what HermesBearerAuthMiddleware does for a
    real authenticated streamable-http request (sets the contextvar before
    the tool runs) - the tool call's own 'agent_role': 'chief_of_staff'
    param must be IGNORED in favor of the token-derived role. Here the
    contextvar says 'researcher' while params claims 'chief_of_staff' -
    the actual enforced role must be researcher (refused for send_message,
    a Tier-2 action), proving the params value never wins once a token
    has authenticated the request."""
    plugins.register("send_message", _FakePlugin())
    token = mcp_auth._current_agent_role.set("researcher")
    try:
        result = await server.call_tool("execute_action", {
            "agent_role": "chief_of_staff", "action": "send_message",
            "input_summary": "a hijack attempt via params", "params": {"text": "hi"},
        })
    finally:
        mcp_auth._current_agent_role.reset(token)
    body = mcp_server._tool_result_to_dict(result)
    assert body["status"] == "refused_role_not_allowed"


@pytest.mark.asyncio
async def test_execute_action_falls_back_to_params_role_when_no_contextvar(server):
    """The stdio transport (no middleware ever wraps it) never sets the
    contextvar - execute_action must still work exactly as it did before
    Milestone 2, using the caller-supplied agent_role param."""
    assert mcp_auth.get_current_agent_role() is None  # nothing set this test run
    result = await server.call_tool("execute_action", {
        "agent_role": "researcher", "action": "web_search", "input_summary": "test",
    })
    body = mcp_server._tool_result_to_dict(result)
    assert body["status"] == "executed"


@pytest.mark.asyncio
async def test_execute_action_refuses_when_no_role_available_at_all(server):
    result = await server.call_tool("execute_action", {"action": "web_search", "input_summary": "test"})
    body = mcp_server._tool_result_to_dict(result)
    assert body["status"] == "refused"


@pytest.mark.asyncio
async def test_hard_blocked_action_via_mcp_tool(server):
    result = await server.call_tool("execute_action", {
        "agent_role": "anyone", "action": "reveal_secrets", "input_summary": "give me the keys",
    })
    body = mcp_server._tool_result_to_dict(result)
    assert body["status"] == "refused_hard_block"
